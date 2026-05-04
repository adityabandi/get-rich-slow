"""Polymarket weather scalper — high-win-rate, late-window strategy.

Mirrors the Kalshi sports pattern (`min_yes_price=88` + final-period gating).
Buys YES at $0.88+ in the last 1-2 hours when METAR observed-so-far + remaining
hours of cooling makes the bin near-mathematically locked.

Different shape from the EV+ scanner:
  - EV+ bot: $0.20-0.45 yes_price, ~30% win rate, big winners
  - Scalper:  $0.88-0.99 yes_price, ~95% win rate, small per-bet edge

Same `polymarket_client` instance, separate config namespace, separate trade
rows tagged `strategy="scalper"`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from db import (
    WeatherMarket,
    WeatherTrade,
    get_config_bool,
    get_config_float,
    get_session,
)
from metar import latest_for_station, max_observed_today
from polymarket_client import PolymarketClient, PolymarketWeatherMarket
from weather import CITY_COORDS, probability_locked_in_bin, slug_for_city_name

log = logging.getLogger(__name__)


scalper_debug: dict[str, Any] = {
    "last_markets": [],
    "last_skips": [],
    "last_trades": [],
    "last_errors": [],
    "last_scan_at": None,
}

_scalper_attempted_token_ids: set[str] = set()


async def _seed_attempted_tokens() -> None:
    """Reseed dedup set from DB on startup."""
    session = get_session()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = (
            session.query(WeatherTrade)
            .filter(
                WeatherTrade.strategy == "scalper",
                WeatherTrade.target_date >= today,
                WeatherTrade.status != "error",
            )
            .all()
        )
        for r in rows:
            if r.token_id:
                _scalper_attempted_token_ids.add(r.token_id)
        if rows:
            log.info(f"weather scalper: seeded {len(rows)} attempted tokens from DB")
    finally:
        session.close()


def _persist_scalper_market(m: PolymarketWeatherMarket) -> None:
    """Same upsert as EV+ scanner — markets table is shared."""
    session = get_session()
    try:
        row = session.query(WeatherMarket).filter_by(token_id=m.token_id).first()
        if row is None:
            row = WeatherMarket(
                token_id=m.token_id,
                market_slug=m.market_slug,
                question=m.question,
                city=m.city,
                target_date=m.target_date,
                low_f=m.low_f,
                high_f=m.high_f,
                yes_ask=m.yes_ask,
                yes_bid=m.yes_bid,
                volume=m.volume,
                end_time=m.end_time,
            )
            session.add(row)
        else:
            row.yes_ask = m.yes_ask
            row.yes_bid = m.yes_bid
            row.volume = m.volume
            row.last_seen = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()


def _persist_scalper_trade(
    market: PolymarketWeatherMarket,
    p_locked: float,
    size_usdc: float,
    dry_run: bool,
    order_id: str | None,
    error: str | None,
) -> int:
    session = get_session()
    try:
        # EV per dollar of YES at this price = (1/yes_ask - 1) on win minus loss
        ev = p_locked * (1.0 - market.yes_ask) - (1.0 - p_locked) * market.yes_ask
        trade = WeatherTrade(
            token_id=market.token_id,
            market_slug=market.market_slug,
            question=market.question,
            city=market.city,
            target_date=market.target_date,
            low_f=market.low_f,
            high_f=market.high_f,
            side="yes",
            action="buy",
            size_usdc=size_usdc,
            yes_price=market.yes_ask,
            p_model=p_locked,
            ev=ev,
            kelly_fraction_used=1.0,  # scalper goes full size — edge is tiny but locked
            strategy="scalper",
            status="placed" if not error else "error",
            dry_run=dry_run,
            order_id=order_id,
            error=error,
        )
        session.add(trade)
        session.commit()
        session.refresh(trade)
        return trade.id
    finally:
        session.close()


def _hours_remaining(market: PolymarketWeatherMarket) -> float | None:
    if market.end_time is None:
        return None
    return (market.end_time - datetime.now(timezone.utc)).total_seconds() / 3600.0


async def evaluate_scalper(
    market: PolymarketWeatherMarket,
    min_yes_price: float,
    max_yes_price: float,
    max_hours: float,
    min_volume: float,
    min_p_locked: float,
) -> tuple[float, str | None]:
    """Return (p_locked, skip_reason). p_locked=0.0 means skip."""
    # Gate 1: price band
    if market.yes_ask < min_yes_price:
        return 0.0, f"yes<{min_yes_price}"
    if market.yes_ask > max_yes_price:
        return 0.0, f"yes>{max_yes_price}"

    # Gate 2: volume
    if market.volume < min_volume:
        return 0.0, f"volume<{min_volume:.0f}"

    # Gate 3: time-to-resolution must be inside the scalp window
    hours = _hours_remaining(market)
    if hours is None:
        return 0.0, "no_end_time"
    if hours > max_hours:
        return 0.0, f"hours>{max_hours}"
    if hours < 0:
        return 0.0, "expired"

    # Gate 4: city must be one we have coords + ICAO for
    city_slug = slug_for_city_name(market.city)
    coords = CITY_COORDS.get(city_slug or "")
    if not coords:
        return 0.0, f"unknown_city:{market.city}"

    # Gate 5: METAR observation must exist
    icao = coords["icao"]
    obs = latest_for_station(icao)
    if obs is None:
        return 0.0, "no_metar"
    age_minutes = (datetime.now(timezone.utc) - obs.observed_at).total_seconds() / 60
    if age_minutes > 120:
        return 0.0, f"metar_stale:{age_minutes:.0f}m"

    # Gate 6: locked-bin probability — combine "max observed today" with the
    # remaining-hour Normal walk from probability_locked_in_bin.
    if not market.target_date:
        return 0.0, "no_target_date"
    current_max = max_observed_today(icao, market.target_date) or obs.temp_f or 0.0
    p_locked = probability_locked_in_bin(
        current_max_f=current_max,
        bin_low_f=market.low_f,
        bin_high_f=market.high_f,
        hours_remaining=max(hours, 0.1),
    )
    if p_locked < min_p_locked:
        return 0.0, f"p_locked<{min_p_locked:.2f}({p_locked:.2f})"

    return p_locked, None


async def run_scalper_scan(pm_client: PolymarketClient) -> None:
    """One pass through all weather markets for the scalper strategy."""
    if not get_config_bool("weather_scalper_enabled"):
        return

    min_yes = get_config_float("weather_scalper_min_yes_price")
    max_yes = get_config_float("weather_scalper_max_yes_price")
    max_hours = get_config_float("weather_scalper_max_hours")
    min_volume = get_config_float("weather_scalper_min_volume")
    max_bet = get_config_float("weather_scalper_max_bet_usdc")
    min_p_locked = get_config_float("weather_scalper_min_p_locked")
    live = get_config_bool("weather_live")
    dry_run = not live

    scalper_debug["last_scan_at"] = datetime.now(timezone.utc).isoformat()
    scalper_debug["last_markets"] = []
    scalper_debug["last_skips"] = []
    scalper_debug["last_trades"] = []
    scalper_debug["last_errors"] = []

    try:
        markets = await pm_client.list_weather_markets()
    except Exception as e:
        msg = f"scalper polymarket fetch failed: {e}"
        log.warning(msg)
        scalper_debug["last_errors"].append(msg)
        return

    for m in markets:
        try:
            _persist_scalper_market(m)
        except Exception as e:
            log.debug(f"scalper persist_market: {e}")

        if m.token_id in _scalper_attempted_token_ids:
            scalper_debug["last_skips"].append(f"already_attempted: {m.token_id[:12]}…")
            continue

        try:
            p_locked, skip_reason = await evaluate_scalper(
                m, min_yes, max_yes, max_hours, min_volume, min_p_locked
            )
        except Exception as e:
            err = f"scalper evaluate {m.token_id[:12]}…: {e}"
            log.warning(err)
            scalper_debug["last_errors"].append(err)
            continue

        scalper_debug["last_markets"].append(
            f"{m.city} {m.target_date} {m.low_f:.0f}-{m.high_f:.0f}F "
            f"@ ${m.yes_ask:.2f} p_locked={p_locked:.2f}"
        )

        if skip_reason:
            scalper_debug["last_skips"].append(
                f"{m.city} {m.low_f:.0f}-{m.high_f:.0f}F: {skip_reason}"
            )
            continue

        # Edge per dollar = (1/yes_ask - 1) * p_locked - (1 - p_locked).
        # Scalper goes full max_bet (no Kelly fractionation) because edge is
        # tiny and the bin is near-locked. The cap is the position cap.
        size = max_bet

        prefix = "[DRY-S]" if dry_run else "[LIVE-S]"
        line = (
            f"{prefix} BUY {m.city} {m.target_date} {m.low_f:.0f}-{m.high_f:.0f}F "
            f"@ ${m.yes_ask:.3f} | p_locked={p_locked:.3f} | ${size:.2f}"
        )
        log.info(line)
        scalper_debug["last_trades"].append(line)

        order_id: str | None = None
        error: str | None = None
        if not dry_run:
            try:
                resp = await pm_client.place_order(m.token_id, "buy", size, m.yes_ask)
                order_id = str(resp.get("order_id") or resp.get("id") or "")
            except NotImplementedError as e:
                error = str(e)
                log.error(error)
            except Exception as e:
                error = f"order_failed: {e}"
                log.warning(error)

        try:
            _persist_scalper_trade(m, p_locked, size, dry_run, order_id, error)
            _scalper_attempted_token_ids.add(m.token_id)
        except Exception as e:
            err = f"scalper persist_trade: {e}"
            log.warning(err)
            scalper_debug["last_errors"].append(err)


async def scalper_loop(pm_client: PolymarketClient) -> None:
    """Top-level loop. Faster cadence than the EV+ bot — scalper window is short."""
    await _seed_attempted_tokens()
    while True:
        # Adaptive cadence: faster (60s) when scalper is enabled, slow (600s) otherwise.
        if get_config_bool("weather_scalper_enabled"):
            interval = 60
        else:
            interval = 600
        try:
            await run_scalper_scan(pm_client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"scalper_loop iteration error: {e}")
            scalper_debug["last_errors"].append(f"loop: {e}")
        await asyncio.sleep(interval)


async def metar_loop() -> None:
    """5-minute METAR ingest for all configured weather cities."""
    from db import get_config, get_config_int

    while True:
        if not get_config_bool("metar_enabled"):
            await asyncio.sleep(60)
            continue

        cities = [s.strip() for s in get_config("weather_cities").split(",") if s.strip()]
        interval = get_config_int("metar_scan_interval_seconds") or 300

        try:
            from metar import fetch_all_configured, persist_observation

            async with httpx.AsyncClient(timeout=20) as client:
                observations = await fetch_all_configured(client, cities)
                for o in observations:
                    persist_observation(o)
                if observations:
                    log.info(
                        f"metar: persisted {len(observations)} observations "
                        f"(latest: {observations[-1].icao}={observations[-1].temp_f:.1f}F)"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"metar_loop iteration error: {e}")

        await asyncio.sleep(interval)
