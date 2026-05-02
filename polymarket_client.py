"""Read-only Polymarket client for weather markets.

v1 is read-only by design — no Polygon wallet signing, no order placement.
The seam for live execution lives in `place_order` which raises
`NotImplementedError` until `weather_live` is enabled (see weather_scanner.py).

Endpoints used:
- Gamma:  https://gamma-api.polymarket.com/markets   (public, no auth)
- CLOB:   https://clob.polymarket.com/book           (public reads, no auth)

The Gamma response shape is documented at https://docs.polymarket.com — the
fields below match what the live API returns today; we treat anything missing
as a soft skip rather than an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Patterns for Polymarket weather temperature-bin questions. We split into
# two passes: first locate the city, then locate the temperature range. This
# tolerates the wide variety of phrasings Polymarket actually uses
# ("between X and Y°F", "X-Y°F", "X to Y°F", "X–Y°F" with en-dash, etc.).
_CITY_RE = re.compile(
    r"(?:highest|high|max(?:imum)?)\s+temp(?:erature)?\s+(?:in\s+)?"
    r"(?P<city>[A-Za-z][A-Za-z\s\.\-]+?)"
    r"(?:\s+(?:be|reach|hit|on|today|tomorrow)\b|\?|,|\s+between\b|\s+\d)",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    r"(?P<low>-?\d{1,3})\s*(?:[–—\-]|to|and)\s*(?P<high>-?\d{1,3})\s*(?:°\s*)?F",
    re.IGNORECASE,
)


@dataclass
class PolymarketWeatherMarket:
    token_id: str
    market_slug: str
    question: str
    city: str
    target_date: str  # YYYY-MM-DD
    low_f: float
    high_f: float
    yes_ask: float  # 0..1 implied probability
    yes_bid: float | None
    volume: float
    end_time: datetime | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class PolymarketClient:
    """Async read-only client for Polymarket Gamma + CLOB."""

    GAMMA_BASE = "https://gamma-api.polymarket.com"
    CLOB_BASE = "https://clob.polymarket.com"

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.AsyncClient(timeout=timeout)
        self._rate_lock = asyncio.Lock()
        self._last_call = 0.0

    async def close(self):
        await self._client.aclose()

    async def _rate_limit(self):
        async with self._rate_lock:
            import time
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < 0.1:
                await asyncio.sleep(0.1 - elapsed)
            self._last_call = time.monotonic()

    async def _get(self, url: str, params: dict | None = None) -> Any:
        for attempt in range(3):
            await self._rate_limit()
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise
                log.warning(f"Polymarket GET {url} failed: {e}; retry {attempt + 1}")
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code >= 500 and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        return None

    async def list_markets(
        self,
        tag: str = "weather",
        limit: int = 200,
        active: bool = True,
        closed: bool = False,
    ) -> list[dict[str, Any]]:
        """List Polymarket markets filtered by tag (default: weather).

        Gamma supports `tag_slug` as a filter; some deployments expose it under
        `tag`. We pass both for forward/back compat.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "active": "true" if active else "false",
            "closed": "true" if closed else "false",
            "tag_slug": tag,
        }
        data = await self._get(f"{self.GAMMA_BASE}/markets", params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    async def get_orderbook(self, token_id: str) -> dict[str, Any] | None:
        """Best bid/ask snapshot for a YES outcome token from CLOB."""
        try:
            return await self._get(f"{self.CLOB_BASE}/book", params={"token_id": token_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    @staticmethod
    def parse_weather_market(raw: dict[str, Any]) -> PolymarketWeatherMarket | None:
        """Parse a Gamma market dict into a typed weather market.

        Returns None if the market doesn't match the temperature-bin pattern,
        or if essential fields (token_id, price) are missing.
        """
        question = raw.get("question") or raw.get("title") or ""
        city_m = _CITY_RE.search(question)
        range_m = _RANGE_RE.search(question)
        if not city_m or not range_m:
            return None

        try:
            low = float(range_m.group("low"))
            high = float(range_m.group("high"))
        except (TypeError, ValueError):
            return None

        if low > high:
            low, high = high, low

        city = city_m.group("city").strip().rstrip(",")

        # Polymarket binary markets expose YES/NO outcome tokens. We always
        # trade YES; the YES token id is the first entry in `clobTokenIds` and
        # the YES outcome price is the first entry in `outcomePrices`.
        token_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids") or []
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except ValueError:
                token_ids = [token_ids]
        if not token_ids:
            return None
        yes_token_id = str(token_ids[0])

        outcome_prices = raw.get("outcomePrices") or raw.get("outcome_prices") or []
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except ValueError:
                outcome_prices = []
        try:
            yes_ask = float(outcome_prices[0]) if outcome_prices else 0.0
        except (TypeError, ValueError):
            yes_ask = 0.0
        if yes_ask <= 0 or yes_ask >= 1:
            return None

        # Resolution date — try a few common fields. Fall back to endDate.
        target_date_raw = (
            raw.get("gameStartTime")
            or raw.get("resolveBy")
            or raw.get("endDate")
            or raw.get("end_date")
        )
        target_date = ""
        end_time: datetime | None = None
        if target_date_raw:
            try:
                # Polymarket uses ISO strings with trailing Z.
                end_time = datetime.fromisoformat(
                    target_date_raw.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                target_date = end_time.date().isoformat()
            except (TypeError, ValueError):
                target_date = str(target_date_raw)[:10]

        volume = 0.0
        for k in ("volumeNum", "volume_num", "volume", "liquidity"):
            v = raw.get(k)
            if v is not None:
                try:
                    volume = float(v)
                    break
                except (TypeError, ValueError):
                    continue

        return PolymarketWeatherMarket(
            token_id=yes_token_id,
            market_slug=str(raw.get("slug") or raw.get("market_slug") or yes_token_id),
            question=question,
            city=city,
            target_date=target_date,
            low_f=low,
            high_f=high,
            yes_ask=yes_ask,
            yes_bid=None,
            volume=volume,
            end_time=end_time,
            raw=raw,
        )

    async def list_weather_markets(self) -> list[PolymarketWeatherMarket]:
        """Fetch and parse all currently-active weather markets."""
        raws = await self.list_markets(tag="weather")
        out: list[PolymarketWeatherMarket] = []
        for r in raws:
            parsed = self.parse_weather_market(r)
            if parsed:
                out.append(parsed)
        return out

    async def place_order(
        self,
        token_id: str,
        side: str,
        size_usdc: float,
        price: float,
    ) -> dict[str, Any]:
        """Place an order. Stub for v1 — needs Polygon EIP-712 signing wired in."""
        raise NotImplementedError(
            "Polymarket order placement requires WEATHER_LIVE=true and a "
            "Polygon wallet (PK env var). v1 is dry-run only."
        )
