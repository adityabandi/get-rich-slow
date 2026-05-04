"""Polymarket client for weather markets — read paths free, signed paths gated.

Read paths (Gamma + CLOB orderbook) are public, no auth, used everywhere.

Live order placement uses `py-clob-client` (Polymarket's official SDK) which
handles EIP-712 signing internally. Required env vars when going live:

- `PK`     — Polygon wallet private key (hex, no 0x prefix or with — both work)
- `WALLET` — Polygon address that holds USDC.e
- `SIG_TYPE` — 0 (EOA, default), 1 (Magic), or 2 (browser proxy)

Weather markets are typically Neg Risk multi-outcome markets, so orders must
be signed against the Neg Risk Exchange — `OrderArgs.neg_risk` is set from
each market's parsed `neg_risk` flag.

One-time approvals must be run before live trading — see
`scripts/polymarket_approve.py`. `verify_approvals()` will refuse to enable
live mode without them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

# Polygon contract addresses (per Polymarket docs, confirmed Apr 2026).
POLYGON_USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYMARKET_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
POLYMARKET_ROUTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
POLYMARKET_CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYGON_CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"

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
    neg_risk: bool = True  # weather markets default to Neg Risk Exchange
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

        # Weather markets default to Neg Risk; trust the Gamma flag if present
        # (`negRisk` is a boolean in the Gamma response). If a market is NOT
        # Neg Risk, it would sign against the regular CTF Exchange instead.
        neg_risk_raw = raw.get("negRisk")
        if neg_risk_raw is None:
            neg_risk_raw = raw.get("neg_risk")
        neg_risk = bool(neg_risk_raw) if neg_risk_raw is not None else True

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
            neg_risk=neg_risk,
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

    # ---- Live trading via py-clob-client (gated by env vars) ----

    _clob = None  # cached ClobClient
    _clob_creds_set = False

    @classmethod
    def _get_clob_client(cls):
        """Lazy-init the ClobClient. Returns None if no PK env var set."""
        if cls._clob is not None:
            return cls._clob
        pk = os.getenv("PK") or os.getenv("POLYMARKET_PRIVATE_KEY")
        wallet = os.getenv("WALLET") or os.getenv("POLYMARKET_WALLET")
        if not pk:
            return None
        try:
            from py_clob_client.client import ClobClient
        except ImportError as e:
            log.warning(f"py-clob-client not installed: {e}")
            return None

        sig_type = int(os.getenv("SIG_TYPE", "0"))
        # Normalize "0x..." or bare hex
        if pk.startswith("0x"):
            pk = pk[2:]

        client = ClobClient(
            CLOB_HOST,
            key=pk,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=sig_type,
            funder=wallet or None,
        )
        cls._clob = client
        return client

    @classmethod
    def _ensure_clob_creds(cls) -> bool:
        """Derive (or load cached) L2 API creds. Idempotent across calls."""
        client = cls._get_clob_client()
        if client is None:
            return False
        if cls._clob_creds_set:
            return True
        # Try cached creds on /data first (EFS-persistent across container restarts)
        creds_path = "/data/polymarket_creds.json"
        if os.path.isdir("/data") and os.path.isfile(creds_path):
            try:
                with open(creds_path) as f:
                    cached = json.load(f)
                from py_clob_client.clob_types import ApiCreds
                client.set_api_creds(
                    ApiCreds(
                        api_key=cached["api_key"],
                        api_secret=cached["api_secret"],
                        api_passphrase=cached["api_passphrase"],
                    )
                )
                cls._clob_creds_set = True
                return True
            except (OSError, ValueError, KeyError) as e:
                log.warning(f"polymarket cached creds unusable: {e}; re-deriving")

        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            cls._clob_creds_set = True
            if os.path.isdir("/data"):
                try:
                    with open(creds_path, "w") as f:
                        json.dump(
                            {
                                "api_key": creds.api_key,
                                "api_secret": creds.api_secret,
                                "api_passphrase": creds.api_passphrase,
                            },
                            f,
                        )
                    os.chmod(creds_path, 0o600)
                except OSError as e:
                    log.warning(f"could not cache polymarket creds: {e}")
            return True
        except Exception as e:
            log.error(f"polymarket creds derivation failed: {e}")
            return False

    @classmethod
    def verify_approvals(cls) -> dict[str, bool]:
        """Read on-chain allowances + approvals. Returns dict of check → ok.

        Refuses to enable live mode if any are False — log clearly and tell
        the user to run scripts/polymarket_approve.py.
        """
        wallet = os.getenv("WALLET") or os.getenv("POLYMARKET_WALLET")
        rpc = os.getenv("POLYGON_RPC") or "https://polygon-rpc.com"
        if not wallet:
            return {"wallet_set": False}
        try:
            from web3 import Web3
        except ImportError:
            log.warning("web3 not installed; skipping on-chain approval check")
            return {"web3_available": False}

        w3 = Web3(Web3.HTTPProvider(rpc))
        if not w3.is_connected():
            return {"rpc_connected": False}

        wallet_cs = Web3.to_checksum_address(wallet)
        max_uint = (1 << 255)  # treat anything ≥ 2^255 as "unlimited"

        usdce_abi = [
            {
                "constant": True,
                "inputs": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"},
                ],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function",
            }
        ]
        ctf_abi = [
            {
                "constant": True,
                "inputs": [
                    {"name": "owner", "type": "address"},
                    {"name": "operator", "type": "address"},
                ],
                "name": "isApprovedForAll",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function",
            }
        ]
        usdce = w3.eth.contract(
            address=Web3.to_checksum_address(POLYGON_USDCE),
            abi=usdce_abi,
        )
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(POLYMARKET_CONDITIONAL_TOKENS),
            abi=ctf_abi,
        )

        spenders = {
            "ctf_exchange": POLYMARKET_CTF_EXCHANGE,
            "neg_risk_exchange": POLYMARKET_NEG_RISK_EXCHANGE,
            "router": POLYMARKET_ROUTER,
        }
        result: dict[str, bool] = {"rpc_connected": True}
        for name, spender in spenders.items():
            spender_cs = Web3.to_checksum_address(spender)
            try:
                allowance = usdce.functions.allowance(wallet_cs, spender_cs).call()
                result[f"usdce_{name}"] = allowance >= max_uint
            except Exception as e:
                log.warning(f"allowance check failed for {name}: {e}")
                result[f"usdce_{name}"] = False
            try:
                approved = ctf.functions.isApprovedForAll(wallet_cs, spender_cs).call()
                result[f"ctf_{name}"] = bool(approved)
            except Exception as e:
                log.warning(f"isApprovedForAll check failed for {name}: {e}")
                result[f"ctf_{name}"] = False
        return result

    async def place_order(
        self,
        token_id: str,
        side: str,
        size_usdc: float,
        price: float,
        neg_risk: bool = True,
    ) -> dict[str, Any]:
        """Place a YES buy/sell against Polymarket CLOB via py-clob-client.

        EIP-712 signing happens inside the SDK. `neg_risk=True` is required
        for weather markets; verify each market's neg_risk flag at the call
        site (already wired in `weather_scanner` and `weather_scalper`).
        """
        if not self._ensure_clob_creds():
            raise NotImplementedError(
                "Polymarket live trading not configured — set PK + WALLET env "
                "vars and run scripts/polymarket_approve.py before flipping "
                "weather_live=true."
            )

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
        except ImportError as e:
            raise NotImplementedError(f"py-clob-client missing: {e}")

        clob = self._get_clob_client()
        assert clob is not None  # _ensure_clob_creds returned True

        # py-clob-client's OrderArgs takes `size` in token units, not USDC.
        # For a YES contract priced at `price`, `size_tokens = size_usdc / price`.
        if price <= 0:
            raise ValueError(f"invalid price {price}")
        size_tokens = round(size_usdc / price, 2)
        if size_tokens < 5.0:
            # Polymarket CLOB minimum order is 5 tokens (≈ $5 face value)
            raise ValueError(
                f"order size {size_tokens:.2f} below 5-token minimum "
                f"(usdc={size_usdc:.2f}, price={price:.3f})"
            )

        side_const = BUY if side.lower() == "buy" else SELL

        def _build_and_post() -> dict[str, Any]:
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size_tokens,
                side=side_const,
            )
            # neg_risk attribute presence depends on SDK version — set if available
            if hasattr(args, "neg_risk"):
                args.neg_risk = neg_risk
            signed = clob.create_order(args)
            resp = clob.post_order(signed, OrderType.GTC)
            return resp

        # ClobClient is sync; offload to a thread so we don't block the event loop.
        return await asyncio.to_thread(_build_and_post)

    async def place_sell_order(
        self, token_id: str, size_usdc: float, price: float
    ) -> dict[str, Any]:
        """Convenience wrapper for stop-loss / manual exits."""
        return await self.place_order(token_id, "sell", size_usdc, price, neg_risk=True)
