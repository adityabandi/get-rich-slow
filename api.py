"""FastAPI backend serving dashboard data and live game tracking."""

import asyncio
import hashlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, func

from db import (
    BalanceSnapshot,
    Opportunity,
    Scan,
    Trade,
    get_all_config,
    get_config_int,
    get_session,
    init_db,
    set_config,
)
from espn import KALSHI_TO_ESPN, get_final_period, get_scoreboard, match_kalshi_to_espn
from kalshi_client import KalshiClient
from scanner import MIN_SCORE_LEAD, _parse_market_prices, market_prices, meets_blowout_tier

# Cached Kalshi data — refreshed at most every 15 seconds
_balance_cache: dict = {"balance": 0, "portfolio_value": 0, "ts": 0.0}
_positions_cache: dict = {"positions": [], "ts": 0.0}
_CACHE_TTL = 15.0

# --- Pydantic response models ---


class StatsResponse(BaseModel):
    total_trades: int
    live_trades: int
    dry_run_trades: int
    error_trades: int
    pending_trades: int
    stopped_out_trades: int
    manual_close_trades: int
    total_cost_cents: int
    total_potential_profit_cents: int
    realized_pnl_cents: int
    wins: int
    losses: int
    win_rate: float
    total_scans: int
    total_opportunities: int
    balance_cents: int
    portfolio_value_cents: int
    open_positions: int
    open_cost_cents: int
    open_potential_profit_cents: int
    total_deposited_cents: int


class TradeResponse(BaseModel):
    id: int
    placed_at: Optional[datetime] = None
    ticker: str
    event_ticker: Optional[str] = None
    title: Optional[str] = None
    side: str
    count: int
    yes_price: int
    cost_cents: int
    potential_profit_cents: int
    status: str
    pnl_cents: Optional[int] = None
    dry_run: bool
    error: Optional[str] = None
    order_id: Optional[str] = None


class TradesListResponse(BaseModel):
    trades: list[TradeResponse]



# --- App ---

log = logging.getLogger(__name__)
_kalshi_client: KalshiClient | None = None


async def _run_scanner_loop():
    """Run the scanner in the background as a native async task."""
    from scanner import run_scanner

    min_price = int(os.getenv("MIN_YES_PRICE", "88"))
    max_bet = int(os.getenv("MAX_BET_AMOUNT_CENTS", "300"))
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
    dry = os.getenv("DRY_RUN", "true").lower() == "true"

    log.info(
        f"Starting scanner: min_price={min_price}c, "
        f"max_bet={max_bet}c, interval={interval}s, dry_run={dry}"
    )
    await run_scanner(
        min_yes_price=min_price,
        max_bet_cents=max_bet,
        poll_interval=interval,
        dry_run=dry,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _kalshi_client
    init_db()

    if os.getenv("KALSHI_API_KEY"):
        key_id = os.environ["KALSHI_API_KEY"]
        key_pem = os.environ.get("KALSHI_PRIVATE_KEY")
        if key_pem:
            _kalshi_client = KalshiClient.from_key_string(key_id, key_pem)
        else:
            key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
            _kalshi_client = KalshiClient.from_key_file(key_id, key_path)

        asyncio.create_task(_run_scanner_loop())
    yield


app = FastAPI(title="Predictions Dashboard API", lifespan=lifespan)

_default_origins = "https://getrich.rager.tech,http://localhost:3777,http://localhost:3000"
_cors_origins = os.getenv("CORS_ORIGINS", _default_origins).split(",")

app.add_middleware(
    CORSMiddleware,  # type: ignore[arg-type]  # starlette typing issue
    allow_origins=[o.strip() for o in _cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/debug/scan-state")
async def debug_scan_state(request: Request, authorization: str | None = Header(None)):
    """Debug: show what the scanner is seeing/doing."""
    _check_cookie_or_token(request, authorization)
    from scanner import scan_debug
    return scan_debug


@app.post("/api/debug/test-bet")
async def test_bet(request: Request, authorization: str = Header(None)):
    """Place a single test contract to verify Kalshi API connection."""
    _check_token(authorization)
    try:
        body = await request.json()
        ticker = body.get("ticker")
        yes_price = body.get("yes_price")
        if not ticker or not yes_price:
            return {"ok": False, "error": "Need ticker and yes_price"}

        import traceback
        from scanner import load_client
        client = load_client()
        bal = await client.get_balance()
        result = await client.create_order(
            ticker=ticker,
            side="yes",
            action="buy",
            count=1,
            yes_price=yes_price,
        )
        return {"ok": True, "balance": bal, "result": result}
    except Exception as e:
        log.error(f"Endpoint error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@app.post("/api/trades/{trade_id}/close")
async def close_trade(trade_id: int, request: Request, authorization: str = Header(None)):
    """Manually close/sell an open position. Optionally specify a limit price."""
    _check_token(authorization)
    try:
        body = await request.json() if await request.body() else {}
        limit_price = body.get("limit_price")  # optional: sell at this price or better

        session = get_session()
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            session.close()
            return {"ok": False, "error": "Trade not found"}
        if trade.status not in ("placed", "filled", "resting"):
            session.close()
            return {"ok": False, "error": f"Trade not open (status: {trade.status})"}

        from scanner import load_client, market_prices
        client = load_client()

        # Determine sell price: use limit_price, or current bid from WS, or fetch from API
        if limit_price:
            sell_price = int(limit_price)
        else:
            live = market_prices.get(trade.ticker, {})
            sell_price = live.get("yes_bid", 0)
            if not sell_price:
                # Fallback: fetch from Kalshi API
                market = await client.get_market(trade.ticker)
                from scanner import _parse_market_prices
                parsed = _parse_market_prices(market)
                sell_price = parsed.get("yes_bid", 0)
            if not sell_price:
                session.close()
                return {"ok": False, "error": "No bid price available — market may be illiquid"}

        result = await client.create_order(
            ticker=trade.ticker,
            side=trade.side,
            action="sell",
            count=trade.count,
            yes_price=sell_price,
        )

        # P&L = (sell_price - buy_price) * count for YES sells
        trade.status = "manual_close"
        trade.pnl_cents = (sell_price - trade.yes_price) * trade.count
        session.commit()
        session.close()

        return {
            "ok": True,
            "sold_at": sell_price,
            "count": trade.count,
            "pnl_cents": trade.pnl_cents,
            "result": result,
        }
    except Exception as e:
        log.error(f"Endpoint error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@app.get("/api/system-health")
async def system_health(request: Request, authorization: str | None = Header(None)):
    """Detailed health check: scanner status, data sources, error counts."""
    _check_cookie_or_token(request, authorization)
    from sofascore import _live_cache as ss_cache, _working_base as ss_base

    health: dict = {
        "status": "ok",
        "kalshi_connected": _kalshi_client is not None,
        "sofascore_base": ss_base,
        "sofascore_cached_sports": list(ss_cache.keys()),
    }

    # Check scanner state
    from scanner import market_prices as mp
    health["ws_prices_tracked"] = len(mp)

    # Check last scan time
    session = get_session()
    from sqlalchemy import func as sqlfunc
    last_scan = session.query(sqlfunc.max(Scan.scanned_at)).scalar()
    health["last_scan"] = str(last_scan) if last_scan else None

    # Open positions
    open_trades = (
        session.query(Trade)
        .filter(Trade.status.in_(("placed", "filled")))
        .all()
    )
    health["open_positions"] = len(open_trades)
    health["open_tickers"] = [t.ticker for t in open_trades]

    # Stop-loss exits
    stopped = (
        session.query(Trade)
        .filter(Trade.status == "stopped_out")
        .count()
    )
    health["total_stop_losses"] = stopped

    # Recent errors
    errors = (
        session.query(Trade)
        .filter(Trade.status == "error")
        .order_by(Trade.placed_at.desc())
        .limit(5)
        .all()
    )
    health["recent_errors"] = [
        {"ticker": e.ticker, "error": e.error, "at": str(e.placed_at)}
        for e in errors
    ]

    # Win/loss
    wins = session.query(Trade).filter(Trade.status == "settled_win").count()
    losses = session.query(Trade).filter(Trade.status == "settled_loss").count()
    health["wins"] = wins
    health["losses"] = losses
    health["win_rate"] = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0

    # Balance
    if _kalshi_client:
        try:
            bal = await _kalshi_client.get_balance()
            health["balance_cents"] = bal.get("balance", 0)
        except Exception:
            health["balance_cents"] = None

    session.close()
    return health


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats(request: Request, authorization: str | None = Header(None)):
    _check_cookie_or_token(request, authorization)
    session = get_session()

    total_trades = session.query(Trade).count()
    live_trades = session.query(Trade).filter(Trade.dry_run == False).count()
    dry_trades = session.query(Trade).filter(Trade.dry_run == True).count()
    error_trades = session.query(Trade).filter(Trade.status == "error", Trade.dry_run == False).count()
    pending_trades = session.query(Trade).filter(Trade.status == "pending", Trade.dry_run == False).count()
    stopped_out_trades = session.query(Trade).filter(Trade.status == "stopped_out").count()
    manual_close_trades = session.query(Trade).filter(Trade.status == "manual_close").count()

    total_cost = (
        session.query(func.sum(Trade.cost_cents)).filter(Trade.dry_run == False).scalar() or 0
    )
    total_potential_profit = (
        session.query(func.sum(Trade.potential_profit_cents))
        .filter(Trade.dry_run == False)
        .scalar()
        or 0
    )

    wins = session.query(Trade).filter(Trade.status == "settled_win").count()
    losses = session.query(Trade).filter(Trade.status == "settled_loss").count()
    settled = wins + losses
    win_rate = (wins / settled * 100) if settled > 0 else 0

    total_scans = session.query(Scan).count()
    total_opportunities = session.query(func.count(func.distinct(Opportunity.ticker))).scalar() or 0

    # Pull balance from cache (refreshes from Kalshi API every 15s)
    global _balance_cache, _positions_cache
    now = time.time()
    if now - _balance_cache["ts"] > _CACHE_TTL:
        try:
            bal_data = await _kalshi_client.get_balance()
            _balance_cache = {
                "balance": bal_data.get("balance", 0),
                "portfolio_value": bal_data.get("portfolio_value", 0),
                "ts": now,
            }
        except Exception as e:
            log.warning(f"Kalshi balance fetch failed: {e}")  # Use stale cache or DB fallback
    balance_cents = _balance_cache["balance"]
    portfolio_value_cents = _balance_cache["portfolio_value"]
    if not balance_cents:
        latest_balance = (
            session.query(BalanceSnapshot).order_by(desc(BalanceSnapshot.recorded_at)).first()
        )
        balance_cents = latest_balance.balance_cents if latest_balance else 0
        portfolio_value_cents = latest_balance.portfolio_value_cents if latest_balance else 0

    # Real P&L from Kalshi: (balance + portfolio) - total deposited
    total_deposited = get_config_int("total_deposited_cents") or 29600
    total_pnl = (balance_cents + portfolio_value_cents) - total_deposited

    # Open positions from Kalshi API (includes manual trades)
    if now - _positions_cache["ts"] > _CACHE_TTL:
        try:
            pos_data = await _kalshi_client.get_positions(settlement_status="unsettled")
            _positions_cache = {
                "positions": pos_data.get("market_positions", []),
                "ts": now,
            }
        except Exception as e:
            log.warning(f"Kalshi positions fetch failed: {e}")  # Use stale cache
    kalshi_positions = [p for p in _positions_cache["positions"] if p.get("position", 0) > 0]
    open_positions = len(kalshi_positions)
    # market_average_price is in dollars (e.g. 0.92), position is count
    # Cost = avg_price_cents * count; Payout = 100c * count
    open_cost = sum(
        int(round(float(p.get("market_average_price", 0)) * 100)) * p.get("position", 0)
        for p in kalshi_positions
    )
    open_potential = sum(p.get("position", 0) * 100 for p in kalshi_positions) - open_cost

    session.close()

    return StatsResponse(
        total_trades=total_trades,
        live_trades=live_trades,
        dry_run_trades=dry_trades,
        error_trades=error_trades,
        pending_trades=pending_trades,
        stopped_out_trades=stopped_out_trades,
        manual_close_trades=manual_close_trades,
        total_cost_cents=total_cost,
        total_potential_profit_cents=total_potential_profit,
        realized_pnl_cents=total_pnl,
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 1),
        total_scans=total_scans,
        total_opportunities=total_opportunities,
        balance_cents=balance_cents,
        portfolio_value_cents=portfolio_value_cents,
        open_positions=open_positions,
        open_cost_cents=open_cost,
        open_potential_profit_cents=open_potential,
        total_deposited_cents=total_deposited,
    )



@app.get("/api/trades", response_model=TradesListResponse)
def get_trades(request: Request, authorization: str | None = Header(None), limit: int = 50, offset: int = 0, include_errors: bool = False, status: str | None = None):
    _check_cookie_or_token(request, authorization)
    session = get_session()
    q = session.query(Trade)
    if status:
        # Allow filtering by specific status (e.g. "error", "pending", "stopped_out")
        q = q.filter(Trade.status == status, Trade.dry_run == False)
    elif not include_errors:
        q = q.filter(Trade.status != "error", Trade.dry_run == False)
    trades = q.order_by(desc(Trade.placed_at)).offset(offset).limit(limit).all()
    result = [
        TradeResponse(
            id=t.id,
            placed_at=t.placed_at,
            ticker=t.ticker,
            event_ticker=t.event_ticker,
            title=t.title,
            side=t.side,
            count=t.count,
            yes_price=t.yes_price,
            cost_cents=t.cost_cents,
            potential_profit_cents=t.potential_profit_cents,
            status=t.status,
            pnl_cents=t.pnl_cents,
            dry_run=t.dry_run,
            error=t.error,
            order_id=t.order_id,
        )
        for t in trades
    ]
    session.close()
    return TradesListResponse(trades=result)


class PnlPoint(BaseModel):
    placed_at: Optional[datetime] = None
    pnl_cents: int
    status: str
    ticker: str
    title: Optional[str] = None
    cost_cents: int
    yes_price: int
    count: int


class PnlHistoryResponse(BaseModel):
    trades: list[PnlPoint]
    total_deposited_cents: int


@app.get("/api/trades/pnl-history", response_model=PnlHistoryResponse)
def get_pnl_history(request: Request, authorization: str | None = Header(None)):
    """Return ALL settled/stopped/closed trades for P&L chart — no limit."""
    _check_cookie_or_token(request, authorization)
    session = get_session()
    settled_statuses = ("settled_win", "settled_loss", "stopped_out", "manual_close")
    trades = (
        session.query(Trade)
        .filter(
            Trade.status.in_(settled_statuses),
            Trade.dry_run == False,
            Trade.pnl_cents.isnot(None),
        )
        .order_by(Trade.placed_at.asc())
        .all()
    )
    result = [
        PnlPoint(
            placed_at=t.placed_at,
            pnl_cents=t.pnl_cents,
            status=t.status,
            ticker=t.ticker,
            title=t.title,
            cost_cents=t.cost_cents or 0,
            yes_price=t.yes_price or 0,
            count=t.count or 0,
        )
        for t in trades
    ]
    total_deposited = get_config_int("total_deposited_cents") or 29600
    session.close()
    return PnlHistoryResponse(trades=result, total_deposited_cents=total_deposited)


async def _get_live_games() -> list[dict]:
    """Fetch all live games across all sports from ESPN + SofaScore, enriched with Kalshi prices."""
    import asyncio
    from sofascore import KALSHI_TO_SOFASCORE, SOFASCORE_SCORE_LEAD, get_all_sofascore_games

    all_games = []
    all_series = set(KALSHI_TO_ESPN.keys()) | set(KALSHI_TO_SOFASCORE.keys())

    # Fetch Kalshi events AND market data for all sports series IN PARALLEL
    kalshi_markets: dict[str, list[dict]] = {}
    kalshi_market_data: dict[str, dict] = {}  # ticker -> market data

    async def fetch_kalshi_series(series: str):
        events = []
        markets = []
        if not _kalshi_client:
            return series, events, markets
        try:
            data = await _kalshi_client.get_events(
                status="open",
                series_ticker=series,
                with_nested_markets=True,
            )
            events = data.get("events", [])
        except Exception as e:
            log.warning(f"Kalshi events fetch failed for {series}: {e}")
        try:
            cursor = None
            while True:
                mdata = await _kalshi_client.get_markets(
                    series_ticker=series,
                    status="open",
                    cursor=cursor,
                )
                markets.extend(mdata.get("markets", []))
                cursor = mdata.get("cursor", "")
                if not cursor:
                    break
        except Exception as e:
            log.warning(f"Kalshi markets fetch failed for {series}: {e}")
        return series, events, markets

    # Fire all Kalshi fetches in parallel
    kalshi_results = await asyncio.gather(
        *[fetch_kalshi_series(s) for s in all_series],
        return_exceptions=True,
    )
    for result in kalshi_results:
        if isinstance(result, Exception):
            continue
        series, events, markets = result
        kalshi_markets[series] = events
        for m in markets:
            t = m.get("ticker", "")
            if t:
                kalshi_market_data[t] = m

    # Deduplicate sport paths (e.g. KXMLBGAME and KXMLBSTGAME both map to baseball/mlb)
    seen_sport_paths: set[str] = set()
    unique_sports: list[tuple[str, str]] = []  # (series, sport_path)
    # Collect all series per sport_path for Kalshi market matching
    series_by_sport: dict[str, list[str]] = {}
    for series, sport_path in KALSHI_TO_ESPN.items():
        series_by_sport.setdefault(sport_path, []).append(series)
        if sport_path not in seen_sport_paths:
            seen_sport_paths.add(sport_path)
            unique_sports.append((series, sport_path))

    # Fetch all ESPN scoreboards in parallel
    async def fetch_espn(primary_series: str, sport_path: str):
        return primary_series, sport_path, await get_scoreboard(sport_path)

    espn_results = await asyncio.gather(
        *[fetch_espn(s, sp) for s, sp in unique_sports],
        return_exceptions=True,
    )

    # Also fetch SofaScore in parallel with ESPN (already done above via gather)
    ss_games_task = asyncio.create_task(get_all_sofascore_games())

    for result in espn_results:
        if isinstance(result, Exception):
            continue
        _primary_series, sport_path, games = result
        for g in games:
            if g.state != "in":
                continue

            min_lead = MIN_SCORE_LEAD.get(sport_path, 5)
            meets_score_lead = g.score_diff >= min_lead
            is_blowout = meets_blowout_tier(g, min_lead)
            is_target = (g.is_in_final_minutes and meets_score_lead) or is_blowout
            is_watching = (
                not is_target
                and g.state == "in"
                and (g.is_in_final_minutes or meets_score_lead or g.is_final_period)
            )

            # Check if we have an active trade on this event
            has_bet = False
            session = get_session()
            for series in series_by_sport[sport_path]:
                bet_count = (
                    session.query(Trade)
                    .filter(
                        Trade.event_ticker.like(f"{series}%"),
                        Trade.status.in_(("placed", "filled")),
                    )
                    .all()
                )
                for t in bet_count:
                    if (
                        g.home_team.upper() in (t.event_ticker or "").upper()
                        or g.away_team.upper() in (t.event_ticker or "").upper()
                    ):
                        has_bet = True
                        break
                if has_bet:
                    break
            session.close()

            game_data: dict = {
                "espn_id": g.espn_id,
                "sport": sport_path,
                "series": _primary_series,
                "home_team": g.home_team,
                "away_team": g.away_team,
                "home_score": g.home_score,
                "away_score": g.away_score,
                "period": g.period,
                "display_clock": g.display_clock,
                "clock_seconds": g.clock_seconds,
                "state": g.state,
                "is_final_minutes": g.is_in_final_minutes,
                "is_target": is_target,
                "is_watching": is_watching,
                "has_bet": has_bet,
                "score_diff": g.score_diff,
                "min_score_lead": min_lead,
                "final_period": g.final_period,
                "kalshi_markets": [],
            }

            # Match Kalshi markets from ALL series for this sport
            seen_tickers: set[str] = set()
            for series in series_by_sport[sport_path]:
                for event in kalshi_markets.get(series, []):
                    title = event.get("title", "")
                    for market in event.get("markets", []):
                        ticker = market.get("ticker", "")
                        if ticker in seen_tickers:
                            continue
                        if market.get("status") not in ("active", "open"):
                            continue
                        matched = match_kalshi_to_espn(ticker, title, [g])
                        if matched:
                            kalshi_code = ticker.split("-")[-1].upper() if "-" in ticker else ""
                            from espn import _espn_to_kalshi_codes

                            espn_team = ""
                            for team in (g.home_team, g.away_team):
                                if kalshi_code in [c.upper() for c in _espn_to_kalshi_codes(team)]:
                                    espn_team = team
                                    break
                            # Price priority: WS real-time > get_markets API > nested event data
                            ws_prices = market_prices.get(ticker, {})
                            real_mkt = kalshi_market_data.get(ticker, {})
                            # Parse dollar-denominated fields from API data
                            real_parsed = _parse_market_prices(real_mkt) if real_mkt else {}
                            nested_parsed = _parse_market_prices(market)
                            yes_bid = ws_prices.get("yes_bid") or real_parsed.get("yes_bid") or nested_parsed.get("yes_bid", 0)
                            yes_ask = ws_prices.get("yes_ask") or real_parsed.get("yes_ask") or nested_parsed.get("yes_ask", 0)
                            vol = ws_prices.get("volume") or real_parsed.get("volume") or nested_parsed.get("volume", 0)
                            game_data["kalshi_markets"].append(
                                {
                                    "ticker": ticker,
                                    "team": espn_team,
                                    "yes_sub_title": market.get("yes_sub_title", ""),
                                    "yes_bid": yes_bid,
                                    "yes_ask": yes_ask,
                                    "volume": vol,
                                }
                            )
                            seen_tickers.add(ticker)

            all_games.append(game_data)

    # --- SofaScore international leagues ---
    try:
        ss_games = await ss_games_task
        for series, games in ss_games.items():
            config = KALSHI_TO_SOFASCORE.get(series, {})
            sport_path = config.get("sport_path", "")
            series_by_sport.setdefault(sport_path, []).append(series)

            for g in games:
                if g.state != "in":
                    continue

                min_lead = SOFASCORE_SCORE_LEAD.get(sport_path, 8)
                meets_score_lead = g.score_diff >= min_lead
                is_blowout = meets_blowout_tier(g, min_lead)
                is_target = (g.is_in_final_minutes and meets_score_lead) or is_blowout
                is_watching = (
                    not is_target
                    and g.state == "in"
                    and (g.is_in_final_minutes or meets_score_lead or g.is_final_period)
                )

                game_data: dict = {
                    "espn_id": g.espn_id,
                    "sport": sport_path,
                    "series": series,
                    "home_team": g.home_team,
                    "away_team": g.away_team,
                    "home_score": g.home_score,
                    "away_score": g.away_score,
                    "period": g.period,
                    "display_clock": g.display_clock,
                    "clock_seconds": g.clock_seconds,
                    "state": g.state,
                    "is_final_minutes": g.is_in_final_minutes,
                    "is_target": is_target,
                    "is_watching": is_watching,
                    "has_bet": False,
                    "score_diff": g.score_diff,
                    "min_score_lead": min_lead,
                    "final_period": g.final_period,
                    "source": "sofascore",
                    "kalshi_markets": [],
                }

                # Match Kalshi markets
                for event in kalshi_markets.get(series, []):
                    title = event.get("title", "")
                    for market in event.get("markets", []):
                        ticker = market.get("ticker", "")
                        if market.get("status") not in ("active", "open"):
                            continue
                        matched = match_kalshi_to_espn(ticker, title, [g])
                        if matched:
                            ws_prices = market_prices.get(ticker, {})
                            real_mkt = kalshi_market_data.get(ticker, {})
                            real_parsed = _parse_market_prices(real_mkt) if real_mkt else {}
                            nested_parsed = _parse_market_prices(market)
                            yes_bid = ws_prices.get("yes_bid") or real_parsed.get("yes_bid") or nested_parsed.get("yes_bid", 0)
                            yes_ask = ws_prices.get("yes_ask") or real_parsed.get("yes_ask") or nested_parsed.get("yes_ask", 0)
                            vol = ws_prices.get("volume") or real_parsed.get("volume") or nested_parsed.get("volume", 0)
                            game_data["kalshi_markets"].append({
                                "ticker": ticker,
                                "team": "",
                                "yes_sub_title": market.get("yes_sub_title", ""),
                                "yes_bid": yes_bid,
                                "yes_ask": yes_ask,
                                "volume": vol,
                            })

                all_games.append(game_data)
    except Exception as e:
        log.warning(f"SofaScore live-games error: {e}")

    return all_games


# Server-side cache for live-games to avoid hammering Kalshi/ESPN APIs
_live_games_cache: dict = {"data": None, "ts": 0.0, "lock": None}
_LIVE_GAMES_TTL = 15  # seconds — dashboard polls every 7s, so at most 1 fresh fetch per 15s


@app.get("/api/live-games")
async def get_live_games(request: Request, authorization: str | None = Header(None)):
    _check_cookie_or_token(request, authorization)
    cache = _live_games_cache
    if cache["lock"] is None:
        cache["lock"] = asyncio.Lock()

    now = time.monotonic()
    # Return cached data if fresh enough
    if cache["data"] is not None and (now - cache["ts"]) < _LIVE_GAMES_TTL:
        return cache["data"]

    async with cache["lock"]:
        # Double-check after acquiring lock (another request may have refreshed)
        now = time.monotonic()
        if cache["data"] is not None and (now - cache["ts"]) < _LIVE_GAMES_TTL:
            return cache["data"]

        all_games = await _get_live_games()
        with_markets = [g for g in all_games if g.get("kalshi_markets")]
        result = {"games": with_markets}
        cache["data"] = result
        cache["ts"] = time.monotonic()
        return result


@app.get("/api/debug/all-series")
async def debug_all_series(request: Request, authorization: str | None = Header(None)):
    """List ALL sports series available on Kalshi."""
    _check_cookie_or_token(request, authorization)
    if not _kalshi_client:
        return {"error": "Kalshi client not initialized"}
    try:
        data = await _kalshi_client.get_series()
        sports = []
        for s in data.get("series", []):
            cat = s.get("category", "")
            ticker = s.get("ticker", "")
            title = s.get("title", "")
            if cat == "Sports" or any(
                kw in ticker.upper() for kw in ["GAME", "FIGHT", "MATCH", "BOUT"]
            ):
                sports.append({"ticker": ticker, "title": title, "category": cat})
        return {"series": sorted(sports, key=lambda x: x["ticker"]), "count": len(sports)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/kalshi-raw")
async def debug_kalshi_raw(request: Request, authorization: str | None = Header(None), series: str = "KXNCAAMBGAME"):
    """Debug: show raw Kalshi API responses for a series to diagnose price issues."""
    _check_cookie_or_token(request, authorization)
    if not _kalshi_client:
        return {"error": "Kalshi client not initialized"}

    results: dict = {"series": series}

    # 1. get_events with nested markets
    try:
        events_data = await _kalshi_client.get_events(
            status="open", series_ticker=series, with_nested_markets=True
        )
        events = events_data.get("events", [])
        results["events_count"] = len(events)
        results["events_sample"] = []
        for event in events[:2]:
            markets = event.get("markets", [])
            results["events_sample"].append({
                "event_ticker": event.get("event_ticker"),
                "title": event.get("title"),
                "markets": [
                    {k: m.get(k) for k in ["ticker", "status", "yes_bid", "yes_ask", "last_price", "volume", "open_interest", "yes_sub_title"]}
                    for m in markets[:4]
                ],
            })
    except Exception as e:
        results["events_error"] = str(e)

    # 2. get_markets (separate endpoint)
    try:
        markets_data = await _kalshi_client.get_markets(
            series_ticker=series, status="open"
        )
        mkts = markets_data.get("markets", [])
        results["markets_count"] = len(mkts)
        results["markets_sample"] = [
            {k: m.get(k) for k in ["ticker", "status", "yes_bid", "yes_ask", "last_price", "volume", "open_interest", "yes_sub_title"]}
            for m in mkts[:4]
        ]
        # Show ALL fields of the first market so we can see what Kalshi actually returns
        if mkts:
            results["first_market_all_fields"] = mkts[0]
    except Exception as e:
        results["markets_error"] = str(e)

    # 3. get_market (single ticker) - if we found any
    try:
        if results.get("markets_sample"):
            ticker = results["markets_sample"][0].get("ticker", "")
            if ticker:
                single = await _kalshi_client.get_market(ticker)
                results["single_market"] = single
    except Exception as e:
        results["single_market_error"] = str(e)

    # 4. Current market_prices dict state
    from scanner import market_prices as mp
    matching = {k: v for k, v in mp.items() if series in k}
    results["market_prices_cached"] = matching

    return results



@app.post("/api/debug/cleanup-phantom-trades")
async def cleanup_phantom_trades(authorization: Optional[str] = Header(None)):
    """Cross-reference DB trades with Kalshi fills to find/fix phantom trades.

    Uses market_ticker param (NOT ticker) for Kalshi fills API.
    Also restores incorrectly-marked phantom trades.
    """
    _check_token(authorization)
    if not _kalshi_client:
        return {"error": "Kalshi client not initialized"}

    # Step 1: Get ALL fills from Kalshi (paginate through everything)
    all_fills: dict[str, float] = {}  # market_ticker -> total filled count
    cursor = None
    page = 0
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        fills_resp = await _kalshi_client.get_fills(**params)
        fill_list = fills_resp.get("fills", [])
        if not fill_list:
            break
        for f in fill_list:
            mt = f.get("market_ticker", "") or f.get("ticker", "")
            count = float(f.get("count_fp", "0") or f.get("count", 0))
            all_fills[mt] = all_fills.get(mt, 0) + count
        cursor = fills_resp.get("cursor")
        page += 1
        if not cursor or page > 20:  # Safety limit
            break

    # Step 2: Get all non-dry-run error trades (previously marked phantom) + settled
    session = get_session()
    trades = (
        session.query(Trade)
        .filter(
            Trade.dry_run == False,
            Trade.status.in_(("error", "settled_win", "settled_loss", "placed", "filled")),
        )
        .order_by(Trade.id)
        .all()
    )

    results = {"checked": 0, "phantom": [], "restored": [], "already_correct": [], "errors": []}

    for trade in trades:
        results["checked"] += 1
        filled = all_fills.get(trade.ticker, 0)

        if filled == 0:
            # Genuinely phantom — no fills on Kalshi
            if trade.status != "error":
                old_status = trade.status
                old_pnl = trade.pnl_cents
                trade.status = "error"
                trade.error = "phantom_trade"
                trade.pnl_cents = 0
                results["phantom"].append({
                    "id": trade.id, "ticker": trade.ticker, "title": trade.title,
                    "old_status": old_status, "old_pnl": old_pnl,
                })
            else:
                results["phantom"].append({
                    "id": trade.id, "ticker": trade.ticker, "title": trade.title,
                    "old_status": "error", "old_pnl": 0,
                })
        else:
            # Real trade — has fills on Kalshi
            if trade.status == "error":
                # Wrongly marked as phantom — need to restore
                # Check market status to determine win/loss
                try:
                    market = await _kalshi_client.get_market(trade.ticker)
                    mkt_status = market.get("status", "")
                    mkt_result = market.get("result", "")

                    if mkt_status in ("finalized", "settled"):
                        if mkt_result == trade.side:
                            trade.status = "settled_win"
                            trade.pnl_cents = trade.potential_profit_cents
                        else:
                            trade.status = "settled_loss"
                            trade.pnl_cents = -trade.cost_cents
                    else:
                        trade.status = "placed"
                        trade.pnl_cents = None

                    trade.error = None
                    results["restored"].append({
                        "id": trade.id, "ticker": trade.ticker, "title": trade.title,
                        "new_status": trade.status, "pnl": trade.pnl_cents,
                        "kalshi_fills": int(filled),
                    })
                except Exception as e:
                    results["errors"].append({"id": trade.id, "error": str(e)})
            else:
                results["already_correct"].append({
                    "id": trade.id, "ticker": trade.ticker, "status": trade.status,
                    "kalshi_fills": int(filled),
                })

    session.commit()
    session.close()

    results["summary"] = {
        "total_fills_on_kalshi": len(all_fills),
        "total_phantom": len(results["phantom"]),
        "total_restored": len(results["restored"]),
        "total_already_correct": len(results["already_correct"]),
    }
    return results


@app.post("/api/debug/fix-error-trades")
async def fix_error_trades(authorization: Optional[str] = Header(None)):
    """Fix error trades by looking up each order_id on Kalshi directly.

    For trades with order_id: calls GET /portfolio/orders/{order_id} to determine
    if the order actually filled. More reliable than bulk fills for settled markets.

    For trades without order_id (409/400 errors): runs reconcile logic via fills API.
    """
    _check_token(authorization)
    if not _kalshi_client:
        return {"error": "Kalshi client not initialized"}

    session = get_session()
    error_trades = (
        session.query(Trade)
        .filter(Trade.status == "error", Trade.dry_run == False)
        .order_by(Trade.placed_at.desc())
        .all()
    )

    results = {"fixed": 0, "skipped": 0, "details": []}

    for trade in error_trades:
        detail = {"id": trade.id, "ticker": trade.ticker, "title": trade.title, "order_id": trade.order_id}

        if trade.order_id:
            # Look up the order directly by ID
            try:
                order = await _kalshi_client.get_order(trade.order_id)
                remaining = order.get("remaining_count")
                order_status = order.get("status", "")
                filled_count = order.get("count", trade.count or 0) - (remaining or 0)

                is_filled = (remaining == 0) or (order_status in ("executed", "resting")) or (filled_count > 0)

                if is_filled:
                    if filled_count > 0 and filled_count != trade.count:
                        trade.count = filled_count
                        trade.cost_cents = filled_count * trade.yes_price
                        trade.potential_profit_cents = filled_count * (100 - trade.yes_price)

                    # Check if market settled
                    market = await _kalshi_client.get_market(trade.ticker)
                    mkt_status = market.get("status", "")
                    mkt_result = market.get("result", "")

                    if mkt_status in ("finalized", "settled"):
                        if mkt_result == trade.side:
                            trade.status = "settled_win"
                            trade.pnl_cents = trade.potential_profit_cents
                        else:
                            trade.status = "settled_loss"
                            trade.pnl_cents = -trade.cost_cents
                    else:
                        trade.status = "placed"
                        trade.pnl_cents = None

                    trade.error = None
                    results["fixed"] += 1
                    detail["result"] = f"fixed → {trade.status}"
                else:
                    results["skipped"] += 1
                    detail["result"] = f"not filled (order_status={order_status}, remaining={remaining})"
            except Exception as e:
                results["skipped"] += 1
                detail["result"] = f"error: {e}"
        else:
            # No order_id — try fills API per market
            try:
                fills_resp = await _kalshi_client.get_fills(market_ticker=trade.ticker)
                fills = fills_resp.get("fills", [])
                filled_count = sum(
                    int(f.get("count", 0))
                    for f in fills
                    if f.get("action") == "buy" and f.get("side") == "yes"
                )
                if filled_count > 0:
                    if filled_count != trade.count:
                        trade.count = filled_count
                        trade.cost_cents = filled_count * trade.yes_price
                        trade.potential_profit_cents = filled_count * (100 - trade.yes_price)
                    market = await _kalshi_client.get_market(trade.ticker)
                    mkt_status = market.get("status", "")
                    mkt_result = market.get("result", "")
                    if mkt_status in ("finalized", "settled"):
                        if mkt_result == trade.side:
                            trade.status = "settled_win"
                            trade.pnl_cents = trade.potential_profit_cents
                        else:
                            trade.status = "settled_loss"
                            trade.pnl_cents = -trade.cost_cents
                    else:
                        trade.status = "placed"
                        trade.pnl_cents = None
                    trade.error = None
                    results["fixed"] += 1
                    detail["result"] = f"fixed via fills → {trade.status}"
                else:
                    results["skipped"] += 1
                    detail["result"] = "no fills, confirmed phantom"
            except Exception as e:
                results["skipped"] += 1
                detail["result"] = f"fills error: {e}"

        results["details"].append(detail)

    session.commit()
    session.close()
    return results


@app.get("/api/debug/test-fills")
async def debug_test_fills(ticker: str = "", authorization: Optional[str] = Header(None)):
    """Debug: test what the Kalshi fills API returns."""
    _check_token(authorization)
    if not _kalshi_client:
        return {"error": "Kalshi client not initialized"}

    results = {}
    # Test 1: get ALL fills (no filter)
    try:
        all_fills = await _kalshi_client.get_fills(limit=20)
        results["all_fills_count"] = len(all_fills.get("fills", []))
        results["all_fills_sample"] = all_fills.get("fills", [])[:5]
        results["all_fills_keys"] = list(all_fills.keys())
    except Exception as e:
        results["all_fills_error"] = str(e)

    # Test 2: get fills filtered by ticker param
    if ticker:
        try:
            filtered = await _kalshi_client.get_fills(ticker=ticker)
            results["filtered_fills"] = filtered
        except Exception as e:
            results["filtered_error"] = str(e)

        # Test 3: try market_ticker param instead
        try:
            filtered2 = await _kalshi_client.get_fills(market_ticker=ticker)
            results["market_ticker_fills"] = filtered2
        except Exception as e:
            results["market_ticker_error"] = str(e)

    return results


@app.get("/api/debug/db-backup")
async def db_backup_download(authorization: Optional[str] = Header(None)):
    """Download the SQLite database file for backup."""
    _check_token(authorization)
    import shutil
    db_url = os.getenv("DATABASE_URL", "sqlite:///predictions.db")
    db_path = db_url.replace("sqlite:///", "") if db_url.startswith("sqlite:///") else "predictions.db"
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="DB not found")
    # Copy to temp to avoid read conflicts
    tmp = db_path + ".download"
    shutil.copy2(db_path, tmp)
    from fastapi.responses import FileResponse
    return FileResponse(tmp, filename="predictions.db", media_type="application/octet-stream")




SPORT_DISPLAY_NAMES = {
    "basketball/nba": "NBA",
    "basketball/mens-college-basketball": "NCAAMB",
    "hockey/nhl": "NHL",
    "football/nfl": "NFL",
    "football/college-football": "NCAAFB",
    "baseball/mlb": "MLB",
    "soccer/eng.1": "EPL",
    "soccer/esp.1": "La Liga",
    "soccer/usa.1": "MLS",
    "mma/ufc": "UFC",
}

# Clock direction per sport: "down" = countdown, "up" = counts up, "none" = no clock
def _get_clock_dir(sport_path: str) -> str:
    """Return clock direction for a sport path."""
    if sport_path.startswith("soccer/"):
        return "up"
    if sport_path.startswith("baseball/"):
        return "none"
    return "down"


# --- Auth constants (must be before auth helpers) ---
_DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
_AUTH_COOKIE = "predictions_auth"
# Stable token derived from the password so it survives container restarts
_AUTH_TOKEN = (
    hashlib.sha256(f"dashboard:{_DASHBOARD_PASSWORD}".encode()).hexdigest()[:32]
    if _DASHBOARD_PASSWORD
    else ""
)


def _check_token(authorization: str | None):
    """Verify Bearer token for mutable endpoints."""
    expected = os.getenv("API_TOKEN", "")
    if not expected:
        raise HTTPException(403, "API_TOKEN not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if authorization.removeprefix("Bearer ") != expected:
        raise HTTPException(401, "Invalid token")


def _check_cookie_or_token(request: Request, authorization: str | None = None):
    """Verify dashboard cookie OR Bearer token. Used for read endpoints."""
    cookie = request.cookies.get(_AUTH_COOKIE, "")
    if _AUTH_TOKEN and secrets.compare_digest(cookie, _AUTH_TOKEN):
        return  # authenticated via dashboard session
    _check_token(authorization)


def _format_final_minutes(clock_dir: str, secs: int) -> str:
    if clock_dir == "none":
        return "final period"
    if clock_dir == "up":
        return f"{secs // 60}th minute"
    mins = secs // 60
    remainder = secs % 60
    return f"{mins}:{remainder:02d} remaining"


@app.get("/api/config")
def get_config_endpoint(request: Request, authorization: str | None = Header(None)):
    _check_cookie_or_token(request, authorization)
    cfg = get_all_config()
    # Config table overrides env var for dry_run
    dr_cfg = cfg.get("dry_run", "")
    if dr_cfg:
        dry_run = dr_cfg.lower() == "true"
    else:
        dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    sports = []
    for sport_path, kalshi_series in sorted([(v, k) for k, v in KALSHI_TO_ESPN.items()]):
        clock_dir = _get_clock_dir(sport_path)
        final_secs = int(cfg.get(f"final_seconds:{sport_path}", "0"))
        if not final_secs:
            final_secs = 4500 if clock_dir == "up" else 300
        lead = int(cfg.get(f"lead:{sport_path}", "0"))
        if not lead and sport_path in MIN_SCORE_LEAD:
            lead = MIN_SCORE_LEAD[sport_path]
        stretch_lead = max(1, lead - (lead * 4 // 10))

        sports.append(
            {
                "sport_path": sport_path,
                "name": SPORT_DISPLAY_NAMES.get(sport_path, sport_path),
                "kalshi_series": kalshi_series,
                "final_period": get_final_period(sport_path),
                "min_score_lead": lead,
                "stretch_score_lead": stretch_lead,
                "clock_direction": clock_dir,
                "final_minutes_desc": _format_final_minutes(clock_dir, final_secs),
                "final_minutes_seconds": (None if clock_dir == "none" else final_secs),
            }
        )

    return {
        "trading": {
            "min_yes_price": int(cfg.get("min_yes_price", "92")),
            "max_bet_cents": int(cfg.get("max_bet_cents", "300")),
            "max_bet_pct": int(cfg.get("max_bet_pct", "0")),
            "max_positions": int(cfg.get("max_positions", "10")),
            "min_volume": int(cfg.get("min_volume", "50")),
            "dry_run": dry_run,
            "stop_loss_price": int(cfg.get("stop_loss_price", "50")),
            "total_deposited_cents": int(cfg.get("total_deposited_cents", "29600")),
        },
        "stretch": {
            "price_min": int(cfg.get("stretch_price_min", "85")),
        },
        "polling": {
            "espn_interval_s": 10,
            "kalshi_scan_interval_s": 5,
            "kalshi_ws": True,
            "db_backup_interval_s": 1800,
        },
        "sports": sports,
    }


class ConfigUpdate(BaseModel):
    key: str
    value: str


# Validation rules: key_prefix -> (type, min, max)
_CONFIG_VALIDATORS: dict[str, tuple[str, int | None, int | None]] = {
    "min_yes_price": ("int", 1, 99),
    "max_yes_price": ("int", 1, 99),
    "max_bet_cents": ("int", 1, 10000),
    "max_bet_pct": ("int", 0, 100),
    "max_positions": ("int", 1, 100),
    "min_volume": ("int", 0, 10000),
    "stop_loss_price": ("int", 0, 99),
    "max_daily_loss": ("int", 0, 100000),
    "total_deposited_cents": ("int", 0, 10000000),
    "stretch_price_min": ("int", 1, 99),
    "tier1_reserved_slots": ("int", 0, 50),
    "tier3_max_slots": ("int", 0, 50),
}
# Prefix-based validators for per-sport keys
_CONFIG_PREFIX_VALIDATORS: dict[str, tuple[str, int | None, int | None]] = {
    "lead:": ("int", 0, 200),
    "final_seconds:": ("int", 0, 10000),
    "tier:": ("int", 1, 3),
}


def _validate_config(key: str, value: str) -> str | None:
    """Validate a config key/value. Returns error message or None if valid."""
    # Boolean keys
    if key == "dry_run":
        if value.lower() not in ("true", "false"):
            return f"dry_run must be 'true' or 'false', got '{value}'"
        return None

    # Exact match validators
    validator = _CONFIG_VALIDATORS.get(key)
    # Prefix match validators
    if not validator:
        for prefix, v in _CONFIG_PREFIX_VALIDATORS.items():
            if key.startswith(prefix):
                validator = v
                break

    if validator:
        vtype, vmin, vmax = validator
        if vtype == "int":
            try:
                iv = int(value)
            except ValueError:
                return f"'{key}' must be an integer, got '{value}'"
            if vmin is not None and iv < vmin:
                return f"'{key}' must be >= {vmin}, got {iv}"
            if vmax is not None and iv > vmax:
                return f"'{key}' must be <= {vmax}, got {iv}"

    return None


@app.put("/api/config")
def update_config(
    body: ConfigUpdate,
    request: Request,
    authorization: str | None = Header(None),
):
    _check_cookie_or_token(request, authorization)
    error = _validate_config(body.key, body.value)
    if error:
        raise HTTPException(400, error)
    set_config(body.key, body.value)
    return {"ok": True, "key": body.key, "value": body.value}


# --- Dashboard auth (cookie-based) ---


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def dashboard_login(body: LoginBody, response: Response):
    if not _DASHBOARD_PASSWORD:
        raise HTTPException(403, "DASHBOARD_PASSWORD not configured")
    if not secrets.compare_digest(body.password, _DASHBOARD_PASSWORD):
        return {"success": False}
    response.set_cookie(
        _AUTH_COOKIE,
        _AUTH_TOKEN,
        httponly=True,
        secure=os.getenv("RAILWAY_ENVIRONMENT") is not None,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return {"success": True}


@app.get("/api/check-auth")
def dashboard_check_auth(request: Request):
    token = request.cookies.get(_AUTH_COOKIE, "")
    return {"authenticated": secrets.compare_digest(token, _AUTH_TOKEN) if _AUTH_TOKEN else False}


@app.post("/api/logout")
def dashboard_logout(response: Response):
    response.delete_cookie(_AUTH_COOKIE, path="/")
    return {"ok": True}


# --- Serve static dashboard (built Next.js export) ---

_DASHBOARD_DIR = Path(__file__).parent / "dashboard_static"

if _DASHBOARD_DIR.is_dir():
    from fastapi.responses import FileResponse

    # Mount _next/ static assets (JS, CSS, media, build manifests)
    _next_dir = _DASHBOARD_DIR / "_next"
    if _next_dir.is_dir():
        app.mount("/_next", StaticFiles(directory=str(_next_dir)), name="next-static")

    # Serve root index.html
    @app.get("/", include_in_schema=False)
    def serve_root():
        return FileResponse(_DASHBOARD_DIR / "index.html")

    # Catch-all for static files and SPA fallback (must be last)
    @app.get("/{path:path}", include_in_schema=False)
    def serve_static_or_spa(path: str):
        # Don't intercept API routes
        if path.startswith("api/") or path == "health":
            raise HTTPException(404)
        # Try exact file (includes .txt RSC payloads Next.js 16 needs)
        requested = _DASHBOARD_DIR / path
        if requested.is_file():
            return FileResponse(requested)
        # Try .html extension (Next.js exports /foo as foo.html)
        html_file = _DASHBOARD_DIR / f"{path}.html"
        if html_file.is_file():
            return FileResponse(html_file)
        # Try as directory with index.html (e.g. /icon/ -> /icon/index.html)
        if requested.is_dir() and (requested / "index.html").is_file():
            return FileResponse(requested / "index.html")
        # SPA fallback
        return FileResponse(_DASHBOARD_DIR / "index.html")
