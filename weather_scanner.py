"""Polymarket weather scanner — the loop the supervisor runs.

Mirrors `scanner.scan_kalshi_with_espn`'s shape:
  1. Fetch active markets.
  2. For each, decide whether to bet using model probability vs market price.
  3. Persist would-be / actual trades and surface debug state.

v1 is dry-run only (no order signing). Set `weather_live=true` in config AND
have a wallet PK in env to enable live placement once that path is wired up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from db import (
    WeatherMarket,
    WeatherTrade,
    get_config,
    get_config_bool,
    get_config_float,
    get_config_int,
    get_session,
)
from polymarket_client import PolymarketClient, PolymarketWeatherMarket
from weather import (
    calibration_weights,
    fetch_visual_crossing_actual,
    gather_forecasts,
    load_calibration,
    persist_forecast,
    probability_in_bin,
    slug_for_city_name,
    update_calibration,
)

log = logging.getLogger(__name__)


# Module-level debug state — surfaced via /api/debug/scan-state. Mirrors
# `scan_debug` in scanner.py.
weather_debug: dict[str, Any] = {
    "last_markets": [],
    "last_skips": [],
    "last_trades": [],
    "last_errors": [],
    "last_scan_at": None,
}

# Intra-process dedup so we don't repeatedly bet the same token in a single
# scanner lifetime. Cleared by restart (scanner reseeds from DB on boot).
_attempted_token_ids: set[str] = set()


@dataclass
class WeatherDecision:
    market: PolymarketWeatherMarket
    p_model: float
    ev: float
    kelly_size_usdc: float
    skip_reason: str | None = None


def _kelly_size(p: float, price: float, kelly_fraction: float, max_usdc: float) -> float:
    """Fractional Kelly for a YES bet. Returns dollars to commit, capped at max.

    Kelly fraction f* = (p - price) / (1 - price) when buying YES at `price`
    with model probability `p`. Multiplied by `kelly_fraction` for safety.
    """
    if price >= 1.0 or price <= 0.0 or p <= price:
        return 0.0
    f_star = (p - price) / (1.0 - price)
    return max(0.0, min(max_usdc, kelly_fraction * f_star * max_usdc))


async def _seed_attempted_tokens() -> None:
    """On startup, seed the dedup set from the DB so a restart doesn't dup bets."""
    session = get_session()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = (
            session.query(WeatherTrade)
            .filter(WeatherTrade.target_date >= today, WeatherTrade.status != "error")
            .all()
        )
        for r in rows:
            if r.token_id:
                _attempted_token_ids.add(r.token_id)
        if rows:
            log.info(f"weather: seeded {len(rows)} attempted tokens from DB")
    finally:
        session.close()


def _persist_market(m: PolymarketWeatherMarket) -> None:
    """Upsert a market snapshot."""
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


def _persist_trade(
    decision: WeatherDecision,
    dry_run: bool,
    order_id: str | None,
    error: str | None,
) -> int:
    session = get_session()
    try:
        m = decision.market
        trade = WeatherTrade(
            token_id=m.token_id,
            market_slug=m.market_slug,
            question=m.question,
            city=m.city,
            target_date=m.target_date,
            low_f=m.low_f,
            high_f=m.high_f,
            side="yes",
            action="buy",
            size_usdc=decision.kelly_size_usdc,
            yes_price=m.yes_ask,
            p_model=decision.p_model,
            ev=decision.ev,
            kelly_fraction_used=get_config_float("weather_kelly_fraction"),
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


async def evaluate_market(
    http_client: httpx.AsyncClient,
    market: PolymarketWeatherMarket,
    sources: list[str],
    cache_ttl: int,
    min_ev: float,
    min_volume: float,
    max_price: float,
    min_hours: float,
    max_hours: float,
    max_bet_usdc: float,
    kelly_fraction: float,
    calibration: dict,
    calibration_min: int,
) -> WeatherDecision:
    """Apply all gates + compute EV. Returns a decision (kelly_size_usdc=0 if skip)."""
    # Gate 1: market metadata sanity
    if market.yes_ask <= 0 or market.yes_ask >= 1:
        return WeatherDecision(market, 0.0, 0.0, 0.0, "invalid_price")
    if market.yes_ask > max_price:
        return WeatherDecision(market, 0.0, 0.0, 0.0, f"price>{max_price}")
    if market.volume < min_volume:
        return WeatherDecision(market, 0.0, 0.0, 0.0, f"volume<{min_volume}")

    # Gate 2: city must be one we have coords for
    city_slug = slug_for_city_name(market.city)
    if not city_slug:
        return WeatherDecision(market, 0.0, 0.0, 0.0, f"unknown_city:{market.city}")

    # Gate 3: time-to-resolution window
    if market.end_time:
        hours_to = (market.end_time - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_to < min_hours:
            return WeatherDecision(market, 0.0, 0.0, 0.0, f"hours<{min_hours}")
        if hours_to > max_hours:
            return WeatherDecision(market, 0.0, 0.0, 0.0, f"hours>{max_hours}")

    if not market.target_date:
        return WeatherDecision(market, 0.0, 0.0, 0.0, "no_target_date")

    # Gate 4: gather forecasts (in parallel) and compute model probability
    forecasts = await gather_forecasts(
        http_client, sources, city_slug, market.target_date, cache_ttl
    )
    if not forecasts:
        return WeatherDecision(market, 0.0, 0.0, 0.0, "no_forecasts")
    for f in forecasts:
        try:
            persist_forecast(f)
        except Exception as e:
            log.debug(f"persist_forecast failed: {e}")

    weights = calibration_weights(
        [f.source for f in forecasts], city_slug, calibration, calibration_min
    )
    p_model = probability_in_bin(forecasts, market.low_f, market.high_f, weights)

    # Gate 5: EV math. ev per $1 of YES bought.
    ev = p_model * (1.0 - market.yes_ask) - (1.0 - p_model) * market.yes_ask
    if ev < min_ev:
        return WeatherDecision(market, p_model, ev, 0.0, f"ev<{min_ev:.2f}")

    size = _kelly_size(p_model, market.yes_ask, kelly_fraction, max_bet_usdc)
    # Floor at 5% of max to skip dust trades whose slippage eats the edge.
    min_size = max(0.10, max_bet_usdc * 0.05)
    if size < min_size:
        return WeatherDecision(market, p_model, ev, size, f"kelly<{min_size:.2f}")

    return WeatherDecision(market, p_model, ev, size, None)


def _today_realized_pnl_usdc() -> float:
    """Sum of pnl_usdc for trades settled today. Used by the daily-loss breaker."""
    today = datetime.now(timezone.utc).date()
    session = get_session()
    try:
        rows = (
            session.query(WeatherTrade)
            .filter(
                WeatherTrade.status.in_(("settled_win", "settled_loss")),
                WeatherTrade.dry_run == False,  # noqa: E712 — sqlalchemy literal
            )
            .all()
        )
        total = 0.0
        for r in rows:
            if r.settled_at and r.settled_at.date() == today and r.pnl_usdc is not None:
                total += float(r.pnl_usdc)
        return total
    finally:
        session.close()


def _redistribute_multi_bin(decisions: list[WeatherDecision], max_bet_usdc: float) -> None:
    """Group accepted decisions by (city, target_date) and renormalize Kelly so
    the sum across adjacent EV+ bins for the same date doesn't exceed max_bet_usdc.
    Mutates kelly_size_usdc in place. Catches "I think it's near 83°F" without
    fighting for one bin.
    """
    groups: dict[tuple[str, str], list[WeatherDecision]] = {}
    for d in decisions:
        if d.skip_reason or d.kelly_size_usdc <= 0:
            continue
        key = (d.market.city or "", d.market.target_date or "")
        groups.setdefault(key, []).append(d)

    for group_decisions in groups.values():
        if len(group_decisions) <= 1:
            continue
        total = sum(d.kelly_size_usdc for d in group_decisions)
        if total <= max_bet_usdc:
            continue
        scale = max_bet_usdc / total
        for d in group_decisions:
            d.kelly_size_usdc *= scale


async def run_weather_scan(
    pm_client: PolymarketClient,
    http_client: httpx.AsyncClient,
) -> None:
    """One pass: fetch markets, evaluate each, place dry-run/live trades.

    Order: evaluate all markets (collect decisions) → multi-bin Kelly redistribution
    → daily-loss circuit breaker → place + persist.
    """
    if not get_config_bool("weather_enabled"):
        return

    sources = [s.strip() for s in get_config("weather_sources").split(",") if s.strip()]
    cities_filter = {s.strip() for s in get_config("weather_cities").split(",") if s.strip()}
    min_ev = get_config_float("weather_min_ev")
    min_volume = get_config_float("weather_min_volume")
    max_price = get_config_float("weather_max_price")
    max_bet = get_config_float("weather_max_bet_usdc")
    kelly_frac = get_config_float("weather_kelly_fraction")
    min_hours = get_config_float("weather_min_hours")
    max_hours = get_config_float("weather_max_hours")
    cache_ttl = get_config_int("weather_forecast_cache_seconds")
    calibration_min = get_config_int("weather_calibration_min")
    live = get_config_bool("weather_live")
    dry_run = not live

    weather_debug["last_scan_at"] = datetime.now(timezone.utc).isoformat()
    weather_debug["last_markets"] = []
    weather_debug["last_skips"] = []
    weather_debug["last_trades"] = []
    weather_debug["last_errors"] = []

    # Daily-loss circuit breaker — only when in live mode (dry-run "loss" isn't real)
    if not dry_run:
        max_daily_loss = get_config_float("weather_max_daily_loss_usdc")
        realized = _today_realized_pnl_usdc()
        if max_daily_loss > 0 and realized < -max_daily_loss:
            msg = (
                f"weather: DAILY LOSS BREAKER fired (realized {realized:.2f} < "
                f"-{max_daily_loss:.2f}); pausing buys for the rest of today"
            )
            log.warning(msg)
            weather_debug["last_errors"].append(msg)
            return

    try:
        markets = await pm_client.list_weather_markets()
    except Exception as e:
        msg = f"polymarket fetch failed: {e}"
        log.warning(msg)
        weather_debug["last_errors"].append(msg)
        return

    calibration = load_calibration()

    accepted: list[WeatherDecision] = []
    for m in markets:
        try:
            _persist_market(m)
        except Exception as e:
            log.debug(f"persist_market failed: {e}")

        city_slug = slug_for_city_name(m.city)
        weather_debug["last_markets"].append(
            f"{m.city} {m.target_date} {m.low_f:.0f}-{m.high_f:.0f}F "
            f"@ ${m.yes_ask:.2f} vol={m.volume:.0f}"
        )

        if cities_filter and (city_slug or "") not in cities_filter:
            weather_debug["last_skips"].append(f"city_filter: {m.city}")
            continue
        if m.token_id in _attempted_token_ids:
            weather_debug["last_skips"].append(f"already_attempted: {m.token_id[:12]}…")
            continue

        try:
            decision = await evaluate_market(
                http_client, m, sources, cache_ttl, min_ev, min_volume, max_price,
                min_hours, max_hours, max_bet, kelly_frac, calibration, calibration_min,
            )
        except Exception as e:
            err = f"evaluate {m.token_id[:12]}…: {e}"
            log.warning(err)
            weather_debug["last_errors"].append(err)
            continue

        if decision.skip_reason:
            weather_debug["last_skips"].append(
                f"{m.city} {m.low_f:.0f}-{m.high_f:.0f}F: {decision.skip_reason}"
            )
            continue
        accepted.append(decision)

    # Multi-bin Kelly: when adjacent bins for same (city, date) all pass,
    # share the budget rather than letting each take full max_bet.
    _redistribute_multi_bin(accepted, max_bet)

    for decision in accepted:
        m = decision.market
        # After redistribution a bin may have fallen below the dust floor.
        min_size = max(0.10, max_bet * 0.05)
        if decision.kelly_size_usdc < min_size:
            weather_debug["last_skips"].append(
                f"{m.city} {m.low_f:.0f}-{m.high_f:.0f}F: post_redistribute<{min_size:.2f}"
            )
            continue

        prefix = "[DRY]" if dry_run else "[LIVE]"
        line = (
            f"{prefix} BUY {m.city} {m.target_date} {m.low_f:.0f}-{m.high_f:.0f}F "
            f"@ ${m.yes_ask:.3f} | p={decision.p_model:.3f} | "
            f"EV {decision.ev:+.3f} | ${decision.kelly_size_usdc:.2f}"
        )
        log.info(line)
        weather_debug["last_trades"].append(line)

        order_id: str | None = None
        error: str | None = None
        if not dry_run:
            try:
                resp = await pm_client.place_order(
                    m.token_id, "buy", decision.kelly_size_usdc, m.yes_ask
                )
                order_id = str(resp.get("order_id") or resp.get("id") or "")
            except NotImplementedError as e:
                error = str(e)
                log.error(error)
            except Exception as e:
                error = f"order_failed: {e}"
                log.warning(error)

        try:
            _persist_trade(decision, dry_run=dry_run, order_id=order_id, error=error)
            _attempted_token_ids.add(m.token_id)
        except Exception as e:
            err = f"persist_trade {m.token_id[:12]}…: {e}"
            log.warning(err)
            weather_debug["last_errors"].append(err)


async def run_weather_settlement_check(http_client: httpx.AsyncClient) -> None:
    """For trades whose target date is past, fetch the actual high and settle.

    Resolves wins/losses, computes pnl, and feeds `forecast_calibration` so the
    next scan uses fresher per-source weights (the "self-learning" loop). After
    the settlement transaction commits, kicks off Claude lesson generation for
    each newly-settled trade off the event loop.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    newly_settled_ids: list[int] = []
    session = get_session()
    try:
        pending = (
            session.query(WeatherTrade)
            .filter(WeatherTrade.status == "placed", WeatherTrade.target_date < today)
            .all()
        )
        if not pending:
            return
        log.info(f"weather: settling {len(pending)} pending trades")

        # Group by (city, target_date) to dedupe API calls
        observed: dict[tuple[str, str], float | None] = {}
        for trade in pending:
            city_slug = slug_for_city_name(trade.city or "") if trade.city else None
            if not city_slug or not trade.target_date:
                trade.status = "error"
                trade.error = "missing_city_or_date"
                continue

            key = (city_slug, trade.target_date)
            if key not in observed:
                observed[key] = await fetch_visual_crossing_actual(
                    http_client, city_slug, trade.target_date
                )
            actual = observed[key]
            if actual is None:
                # Leave for next pass
                continue

            # Win condition: low - 0.5 ≤ actual < high + 0.5  (matches probability_in_bin)
            won = (trade.low_f - 0.5) <= actual < (trade.high_f + 0.5)
            trade.actual_high_f = actual
            trade.status = "settled_win" if won else "settled_loss"
            trade.settled_at = datetime.now(timezone.utc)
            if won:
                # YES paid `yes_price` per share; payout is $1. Profit per $1 = (1/yp - 1) * size.
                trade.pnl_usdc = (trade.size_usdc / trade.yes_price) - trade.size_usdc
            else:
                trade.pnl_usdc = -trade.size_usdc
            newly_settled_ids.append(trade.id)

            # Update calibration for each source that contributed (we replay
            # `weather_sources` since we don't have per-trade source breakdown).
            sources = [s.strip() for s in get_config("weather_sources").split(",") if s.strip()]
            outcome = 1 if won else 0
            for src in sources:
                try:
                    update_calibration(src, city_slug, trade.p_model or 0.0, outcome)
                except Exception as e:
                    log.debug(f"calibration update failed for {src}/{city_slug}: {e}")

        session.commit()
    finally:
        session.close()

    # Lessons run AFTER commit so a model error never rolls back settlement,
    # and on a worker thread so the synchronous Anthropic SDK doesn't block
    # the asyncio event loop.
    if newly_settled_ids:
        await asyncio.to_thread(_write_lessons_for_trades, newly_settled_ids)


def _write_lessons_for_trades(trade_ids: list[int]) -> None:
    """Synchronous helper that runs in a worker thread (one Anthropic call per trade)."""
    try:
        from weather_lessons import generate_lesson
    except ImportError as e:
        log.warning(f"weather lessons import failed: {e}")
        return

    session = get_session()
    try:
        trades = session.query(WeatherTrade).filter(WeatherTrade.id.in_(trade_ids)).all()
    finally:
        session.close()

    for trade in trades:
        try:
            lesson = generate_lesson(trade)
            if lesson is not None:
                log.info(
                    f"weather lesson [{trade.city} {trade.target_date} "
                    f"{trade.low_f:.0f}-{trade.high_f:.0f}F]: {lesson.lesson[:120]}"
                )
        except Exception as e:
            log.warning(f"weather lesson generation failed for trade {trade.id}: {e}")


async def run_weather_stop_loss_loop(pm_client: PolymarketClient) -> None:
    """Periodically check open weather positions; sell if YES bid drops below
    `weather_stop_loss_price`. Mirrors `scanner.check_stop_losses` (line 981).

    No-op until live trading is on — sell orders go through `place_sell_order`
    which raises NotImplementedError until py-clob-client is wired up.
    """
    while True:
        try:
            interval = get_config_int("weather_stop_loss_interval_seconds") or 60
            stop_price = get_config_float("weather_stop_loss_price")
            live = get_config_bool("weather_live")

            if not live or stop_price <= 0:
                await asyncio.sleep(interval)
                continue

            session = get_session()
            try:
                opens = (
                    session.query(WeatherTrade)
                    .filter(
                        WeatherTrade.status == "placed",
                        WeatherTrade.dry_run == False,  # noqa: E712
                    )
                    .all()
                )
            finally:
                session.close()

            for trade in opens:
                try:
                    book = await pm_client.get_orderbook(trade.token_id)
                    if not book:
                        continue
                    bids = book.get("bids") or []
                    if not bids:
                        continue
                    top_bid = float(bids[0].get("price") or 0)
                    if top_bid <= 0 or top_bid > stop_price:
                        continue
                    log.warning(
                        f"weather stop-loss: SELL {trade.token_id[:12]}… "
                        f"({trade.city} {trade.low_f:.0f}-{trade.high_f:.0f}F) "
                        f"top_bid={top_bid:.3f} <= stop={stop_price:.3f}"
                    )
                    try:
                        await pm_client.place_sell_order(
                            trade.token_id, trade.size_usdc, top_bid
                        )
                        # Mark as stopped_out — settlement check ignores anything
                        # not in 'placed' status, so we won't double-process.
                        sess = get_session()
                        try:
                            t = sess.get(WeatherTrade, trade.id)
                            if t is not None:
                                t.status = "stopped_out"
                                t.settled_at = datetime.now(timezone.utc)
                                t.pnl_usdc = (top_bid * t.size_usdc / t.yes_price) - t.size_usdc
                                sess.commit()
                        finally:
                            sess.close()
                    except NotImplementedError:
                        pass  # silent until live trading wired
                    except Exception as e:
                        log.warning(f"stop-loss sell failed for {trade.token_id[:12]}…: {e}")
                except Exception as e:
                    log.debug(f"stop-loss check error: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"stop_loss_loop iteration error: {e}")
        await asyncio.sleep(get_config_int("weather_stop_loss_interval_seconds") or 60)


async def weather_loop(pm_client: PolymarketClient) -> None:
    """Top-level loop wired into scanner.run_scanner via _supervise."""
    await _seed_attempted_tokens()
    async with httpx.AsyncClient(timeout=20) as http_client:
        while True:
            interval = get_config_int("weather_scan_interval_seconds") or 600
            try:
                await run_weather_scan(pm_client, http_client)
                await run_weather_settlement_check(http_client)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"weather_loop iteration error: {e}")
                weather_debug["last_errors"].append(f"loop: {e}")
            await asyncio.sleep(interval)
