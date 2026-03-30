"""
Kalshi Sports Market Scanner

Scans for sports prediction markets where:
  1. Yes price >= 88 cents (outcome nearly decided)
  2. ESPN confirms the game is in its FINAL MINUTES
     (4th quarter <=5 min, 9th inning, 2nd half final minutes, etc)
  3. Sufficient liquidity to trade

Uses ESPN live scoreboard to verify game state, so we only buy
when a game is truly almost over - not just pre-game favorites.

Strategy: Buy Yes at 88-99c on nearly-finished games,
collect $1 at settlement. High volume, high win rate.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from db import (
    BalanceSnapshot,
    Opportunity,
    Scan,
    Trade,
    get_config_int,
    get_session,
    init_db,
)
from espn import (
    game_meets_timing,
    get_categorized_games,
    match_kalshi_to_espn,
    merge_final_periods,
)
from kalshi_client import KalshiClient, KalshiWebSocket
from sofascore import (
    KALSHI_TO_SOFASCORE,
    SOFASCORE_FINAL_PERIOD,
    SOFASCORE_SCORE_LEAD,
    get_all_sofascore_games,
    get_sofascore_series,
)

# Merge SofaScore final periods into ESPN's lookup so GameState works
merge_final_periods(SOFASCORE_FINAL_PERIOD)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scanner.log"),
    ],
)
log = logging.getLogger(__name__)

# Module-level market prices dict, populated by WS/API in run_scanner
market_prices: dict[str, dict] = {}

# Debug state — exposed via /api/debug/scan-state endpoint
scan_debug: dict = {"last_opps": [], "last_skips": [], "last_errors": [], "espn_cache_keys": []}

# Stop-loss retry tracking: ticker -> attempt count
_stop_loss_attempts: dict[str, int] = {}


def _parse_market_prices(market: dict) -> dict:
    """Extract yes_bid/yes_ask/volume from Kalshi market data.

    Kalshi API v2 uses dollar-denominated string fields (*_dollars, *_fp).
    The old integer fields (yes_bid, yes_ask, volume) are null.
    Returns values in CENTS (int) for internal use.
    """
    # Try new dollar fields first, fall back to legacy integer fields
    yes_bid_str = market.get("yes_bid_dollars") or ""
    yes_ask_str = market.get("yes_ask_dollars") or ""
    volume_str = market.get("volume_fp") or ""
    last_price_str = market.get("last_price_dollars") or ""

    if yes_bid_str:
        yes_bid = int(round(float(yes_bid_str) * 100))
    else:
        yes_bid = market.get("yes_bid") or 0

    if yes_ask_str:
        yes_ask = int(round(float(yes_ask_str) * 100))
    else:
        yes_ask = market.get("yes_ask") or 0

    if volume_str:
        volume = int(float(volume_str))
    else:
        volume = market.get("volume") or 0

    if last_price_str:
        last_price = int(round(float(last_price_str) * 100))
    else:
        last_price = market.get("last_price") or 0

    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "volume": volume,
        "last_price": last_price,
    }

# Only bet on games expiring within this many minutes.
# Games typically last 2-3 hours. If expected expiry is 15 min away,
# we're in the final stretch - 4th quarter, 9th inning, etc.
MAX_MINUTES_TO_EXPIRY = 15

# Minimum volume on the market to ensure there's liquidity
MIN_VOLUME = 100

# Minimum score lead by sport to filter out close games that could flip
MIN_SCORE_LEAD = {
    # Basketball — 12pt lead w/ 3:00 left ≈ 2% comeback rate
    "basketball/nba": 12,
    "basketball/mens-college-basketball": 10,
    "basketball/womens-college-basketball": 10,
    # Football — 14pts = two-score game
    "football/nfl": 14,
    "football/college-football": 14,
    # Hockey — 2 goals in 3:00 is ~2% even with pulled goalie
    "hockey/nhl": 2,
    "hockey/mens-college-hockey": 2,
    # Baseball — 4 runs: grand slam only ties, need extra run
    "baseball/mlb": 4,
    "baseball/college-baseball": 4,
    # Basketball (additional)
    "basketball/wnba": 10,
    "basketball/fiba": 10,
    # Lacrosse — minor sport, be conservative
    "lacrosse/mens-college-lacrosse": 5,
    "lacrosse/pll": 5,
    # Australian Football — 5 goal lead (~30 pts)
    "australian-football/afl": 30,
    # Soccer — all leagues use 2-goal lead
    "soccer/eng.1": 2,
    "soccer/esp.1": 2,
    "soccer/ger.1": 2,
    "soccer/fra.1": 2,
    "soccer/ita.1": 2,
    "soccer/ned.1": 2,
    "soccer/por.1": 2,
    "soccer/bel.1": 2,
    "soccer/cze.1": 2,
    "soccer/eng.2": 2,
    "soccer/eng.w.1": 2,
    "soccer/sco.1": 2,
    "soccer/tur.1": 2,
    "soccer/sui.1": 2,
    "soccer/ger.2": 2,
    "soccer/ita.2": 2,
    "soccer/den.1": 2,
    "soccer/gre.1": 2,
    "soccer/aut.1": 2,
    "soccer/swe.1": 2,
    "soccer/nor.1": 2,
    "soccer/fin.1": 2,
    "soccer/pol.1": 2,
    "soccer/cro.1": 2,
    "soccer/rus.1": 2,
    "soccer/ukr.1": 2,
    "soccer/uefa.champions": 2,
    "soccer/uefa.europa": 2,
    "soccer/uefa.europa.conf": 2,
    "soccer/eng.fa": 2,
    "soccer/eng.league_cup": 2,
    "soccer/esp.copa_del_rey": 2,
    "soccer/ita.coppa_italia": 2,
    "soccer/ger.dfb_pokal": 2,
    "soccer/fra.coupe_de_france": 2,
    "soccer/esp.super_cup": 2,
    "soccer/concacaf.champions": 2,
    "soccer/fifa.cwc": 2,
    "soccer/conmebol.libertadores": 2,
    "soccer/conmebol.sudamericana": 2,
    "soccer/usa.1": 2,
    "soccer/usa.nwsl": 2,
    "soccer/usa.usl.c": 2,
    "soccer/mex.1": 2,
    "soccer/bra.1": 2,
    "soccer/arg.1": 2,
    "soccer/col.1": 2,
    "soccer/per.1": 2,
    "soccer/ecu.1": 2,
    "soccer/aus.1": 2,
    "soccer/jpn.1": 2,
    "soccer/chn.1": 2,
    "soccer/ksa.1": 2,
    "soccer/isr.1": 2,
    "soccer/esp.2": 2,
    "soccer/ind.1": 2,
}
# Merge SofaScore international league score leads
MIN_SCORE_LEAD.update(SOFASCORE_SCORE_LEAD)

# ── Sport Priority Tiers ──────────────────────────────────────
# Tier 1 (premium): Major US sports + top European soccer
# Tier 2 (standard): Other soccer leagues, secondary US sports
# Tier 3 (filler): Minor leagues, slow settlement — only bet when no T1/T2 available
SPORT_TIERS: dict[str, int] = {
    # Tier 1: Premium
    "basketball/nba": 1,
    "basketball/mens-college-basketball": 1,
    "hockey/nhl": 1,
    "baseball/mlb": 1,
    "football/nfl": 1,
    "football/college-football": 1,
    "soccer/eng.1": 1,
    "soccer/esp.1": 1,
    "soccer/ger.1": 1,
    "soccer/ita.1": 1,
    "soccer/fra.1": 1,
    "soccer/usa.1": 1,
    "soccer/uefa.champions": 1,
    "soccer/uefa.europa": 1,
    # UFC/Cricket disabled — removed from KALSHI_TO_ESPN
    # Tier 3: Filler (slow settlement / minor / less reliable data)
    "baseball/college-baseball": 3,
    "hockey/mens-college-hockey": 3,
    "hockey/khl": 3,          # SofaScore data — caused $27 wrong-team loss
    "lacrosse/mens-college-lacrosse": 3,
    "lacrosse/pll": 3,
    "soccer/usa.usl.c": 3,
    # Everything else defaults to Tier 2
}


def _get_sport_tier(sport_path: str) -> int:
    """Get priority tier for a sport. Config override → hardcoded → default 2."""
    db_tier = get_config_int(f"tier:{sport_path}")
    if db_tier in (1, 2, 3):
        return db_tier
    return SPORT_TIERS.get(sport_path, 2)


# ── Two-Tier Entry System ──────────────────────────────────────
# Tier 1 (normal): standard lead + standard timing (configured per-sport)
# Tier 2 (blowout): bigger lead → enter earlier (game is essentially decided)
#
# Format: sport_prefix → (blowout_lead_multiplier, countdown_secs, countup_secs)
# - blowout_lead_multiplier: multiply normal lead by this (e.g. 2.5x = 20pts if normal is 8)
# - countdown_secs: enter with this much time remaining (basketball/hockey/football)
# - countup_secs: enter at this elapsed time (soccer)
# Blowout tiers: (lead_multiplier, countdown_seconds, countup_seconds)
# Generic tiers keyed by sport prefix
BLOWOUT_TIERS: dict[str, tuple[float, int, int]] = {
    "basketball": (2.0, 720, 0),     # 24pts (12*2), 12:00 remaining in Q4
    "football": (2.5, 300, 0),       # 35pts (14*2.5), 5:00 remaining
    "hockey": (3.0, 300, 0),         # 6 goals (2*3), 5:00 remaining
    "soccer": (2.0, 0, 4200),        # 4 goals (2*2), 70th minute
    "baseball": (2.5, 0, 0),         # 10 runs (4*2.5), any time in final period
    "lacrosse": (2.0, 300, 0),       # 10 goals (5*2), 5:00 remaining
}

# Exact sport_path overrides: (absolute_lead, countdown_seconds, countup_seconds)
# These use absolute lead values (not multipliers)
BLOWOUT_OVERRIDES: dict[str, tuple[int, int, int]] = {
    "basketball/mens-college-basketball": (22, 300, 0),   # 22pts, 5:00 remaining
    "basketball/womens-college-basketball": (22, 300, 0),  # 22pts, 5:00 remaining
    "basketball/cba": (30, 480, 0),                        # 30pts, 8:00 remaining
}


def _get_blowout_tier(sport_path: str) -> tuple[float, int, int] | None:
    """Get blowout tier config for a sport, or None if no blowout tier."""
    # Check exact overrides first (handled separately in meets_blowout_tier)
    if sport_path in BLOWOUT_OVERRIDES:
        return None  # Signal to use override path
    for prefix, tier in BLOWOUT_TIERS.items():
        # Use startswith or exact segment match to avoid "australian-football" matching "football"
        if sport_path.startswith(prefix + "/") or sport_path == prefix:
            return tier
    return None


def meets_blowout_tier(game, min_lead: int) -> bool:
    """Check if a game meets the blowout tier (bigger lead, earlier entry)."""
    # Check exact overrides first (absolute lead, not multiplied)
    override = BLOWOUT_OVERRIDES.get(game.sport_path)
    if override:
        abs_lead, cd_secs, cu_secs = override
        if game.score_diff < abs_lead:
            return False
        if not game.is_final_period or not game.is_live:
            return False
        # OT: cap timing at 120s for non-soccer countdown sports
        if game.is_overtime and "soccer" not in game.sport_path and "baseball" not in game.sport_path:
            return game.clock_seconds <= 60
        if "soccer" in game.sport_path:
            return game.clock_seconds >= cu_secs
        return game.clock_seconds <= cd_secs

    tier = _get_blowout_tier(game.sport_path)
    if not tier:
        return False

    lead_mult, cd_secs, cu_secs = tier
    blowout_lead = int(min_lead * lead_mult)

    # Must have the bigger lead
    if game.score_diff < blowout_lead:
        return False

    # Must be in final period
    if not game.is_final_period:
        return False

    # Must be live
    if not game.is_live:
        return False

    # Hockey shootout (period > OT) — coin flip, never bet
    if "hockey" in game.sport_path and game.period > game.final_period + 1:
        return False

    # Baseball: require bottom/mid half-inning (same safety as is_in_final_minutes)
    if "baseball" in game.sport_path:
        if game.is_overtime:
            # Extra innings: require bottom/mid, not top
            detail_lower = game.status_detail.lower()
            if "bot" in detail_lower or "mid" in detail_lower:
                return True
            if "top" in detail_lower and game.home_score > game.away_score:
                return True
            return False
        return True

    # OT: cap timing at 120s for non-soccer countdown sports
    if game.is_overtime and "soccer" not in game.sport_path:
        return game.clock_seconds <= 120

    # Soccer extra time: require >= 115min (last 5 min of ET)
    if "soccer" in game.sport_path and game.is_overtime:
        return game.clock_seconds >= 6900  # 115 * 60

    # Check timing (more lenient than normal tier)
    if "soccer" in game.sport_path:
        return game.clock_seconds >= cu_secs
    return game.clock_seconds <= cd_secs


# Sports game series on Kalshi - these are individual game markets
# (not futures/championships which have long expiry windows)
SPORTS_GAME_SERIES = [
    # --- Basketball ---
    "KXNBAGAME",           # NBA
    "KXWNBAGAME",          # WNBA
    "KXNCAAMBGAME",        # Men's college basketball
    "KXNCAAWBGAME",        # Women's college basketball
    "KXFIBAGAME",          # FIBA international
    # --- Football ---
    "KXNFLGAME",           # NFL
    "KXNCAAFBGAME",        # College football (FBS)
    "KXNCAAFCSGAME",       # College football (FCS)
    # --- Hockey ---
    "KXNHLGAME",           # NHL
    "KXNCAAHOCKEYGAME",    # College hockey
    # --- Baseball ---
    "KXMLBGAME",           # MLB
    # "KXMLBSTGAME",       # MLB spring training — disabled (non-competitive, unusual rules)
    # "KXNCAABBGAME",      # College baseball — disabled, Kalshi settlement too slow
    # --- MMA / Boxing ---
    "KXUFCFIGHT",          # UFC
    # --- Soccer: Top 5 European leagues ---
    "KXEPLGAME",           # English Premier League
    "KXLALIGAGAME",        # La Liga (Spain)
    "KXBUNDESLIGAGAME",    # Bundesliga (Germany)
    "KXLIGUE1GAME",        # Ligue 1 (France)
    "KXSERIEAGAME",        # Serie A (Italy)
    # --- Soccer: Other European ---
    "KXEREDIVISIEGAME",    # Eredivisie (Netherlands)
    "KXLIGAPORTUGALGAME",  # Liga Portugal
    "KXBELGIANPLGAME",     # Belgian Pro League
    "KXCZEFLGAME",         # Czech First League
    "KXEFLCHAMPIONSHIPGAME",  # EFL Championship (England 2nd div)
    "KXEWSLGAME",          # England Women's Super League
    "KXSCOTTISHPREMGAME",  # Scottish Premiership
    "KXSUPERLIGGAME",      # Turkish Super Lig
    "KXSWISSSUPERLGAME",   # Swiss Super League
    "KXBUNDESLIGA2GAME",   # Bundesliga 2 (Germany 2nd div)
    "KXSERIEBGAME",        # Serie B (Italy 2nd div)
    "KXDANISHSUPERLIGAGAME",  # Danish Superliga
    "KXSUPERLEAGUEGREECEGAME",  # Super League Greece
    "KXAUSTBUNDESLIGAGAME",  # Austrian Bundesliga
    "KXALLSVENSKANGAME",   # Allsvenskan (Sweden)
    "KXELITESERIEGAME",    # Eliteserien (Norway)
    "KXVEIKKAUSLIIGAGAME", # Veikkausliiga (Finland)
    "KXEKSTRAKLASAGAME",   # Ekstraklasa (Poland)
    "KXHNLGAME",           # HNL (Croatia)
    "KXRPLGAME",           # Russian Premier League
    "KXUKRPLGAME",         # Ukrainian Premier League
    # --- Soccer: European cups ---
    "KXUCLGAME",           # UEFA Champions League
    "KXUELGAME",           # UEFA Europa League
    "KXUECLGAME",          # UEFA Europa Conference League
    "KXFACUPGAME",         # FA Cup (England)
    "KXEFLCUPGAME",        # EFL Cup (League Cup)
    "KXCOPADELREYGAME",    # Copa del Rey (Spain)
    "KXCOPPAITALIAGAME",   # Coppa Italia
    "KXDFBPOKALGAME",      # DFB Pokal (Germany)
    "KXCOUPEDEFRANCEGAME", # Coupe de France
    "KXESPSUPERCUPGAME",   # Spanish Super Cup
    "KXCONCACAFCCUPGAME",  # CONCACAF Champions Cup
    "KXCLUBWCGAME",        # FIFA Club World Cup
    "KXCOPALIBERTADORESGAME",  # Copa Libertadores
    "KXCOPASUDAMERICANAGAME",  # Copa Sudamericana
    # --- Soccer: Americas ---
    "KXMLSGAME",           # MLS
    "KXNWSLGAME",          # NWSL (women's)
    "KXUSLCHAMPGAME",      # USL Championship
    "KXLIGAMXGAME",        # Liga MX (Mexico)
    "KXBRASILEIROGAME",    # Brasileirão (Brazil)
    "KXARGPREMDIVGAME",    # Argentina Primera División
    "KXDIMAYORGAME",       # Liga DIMAYOR (Colombia)
    "KXPERLIGA1GAME",      # Liga 1 (Peru)
    "KXLIGAPROFGAME",      # LigaPro (Ecuador)
    # --- Soccer: Asia / Middle East ---
    "KXJLEAGUEGAME",       # J-League (Japan)
    "KXCHNSLGAME",         # Chinese Super League
    "KXSAUDIPLGAME",       # Saudi Pro League
    "KXISRAELIPLGAME",     # Israeli Premier League
    # --- Soccer: Other European ---
    "KXLALIGA2GAME",       # La Liga 2 (Spain 2nd div)
    # --- Soccer: Other ---
    "KXALEAGUEGAME",       # A-League (Australia)
    "KXINDSLGAME",         # Indian Super League
    # --- Australian Football ---
    "KXAFLGAME",           # AFL
    # --- Lacrosse — disabled, Kalshi settlement too slow ---
    # "KXNCAALAXGAME",     # College lacrosse
    # "KXNCAAMLAXGAME",    # Men's college lacrosse
    # "KXLAXGAME",         # Lacrosse
    # "KXPLLGAME",         # Premier Lacrosse League
    # --- Cricket — disabled (lead=0 is structurally wrong for T20 chase format) ---
    # "KXIPLGAME",         # IPL (Indian Premier League)
    # --- Tennis --- disabled (no ESPN mapping, wastes API calls)
    # "KXTENNISGAME",
    # --- SofaScore-only leagues (double-safety + stop-loss + $1k volume protect) ---
    *get_sofascore_series(),
]

# Series to NEVER bet on even if discovered dynamically
# (Tier system handles priority, double-safety + stop-loss + $1k volume protect the rest)
DISABLED_SERIES: set[str] = set()


def load_client() -> KalshiClient:
    key_id = os.environ["KALSHI_API_KEY"]
    # Support private key as env var (for ECS) or file path (for local)
    key_pem = os.environ.get("KALSHI_PRIVATE_KEY")
    if key_pem:
        return KalshiClient.from_key_string(key_id, key_pem)
    key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    return KalshiClient.from_key_file(key_id, key_path)


async def find_sports_game_series(client: KalshiClient) -> list[str]:
    """Discover sports game series (not futures/awards)."""
    series_data = await client.get_series()
    game_tickers = []
    for s in series_data.get("series", []):
        ticker = s.get("ticker", "")
        # Skip explicitly disabled series
        if any(ticker.startswith(d) for d in DISABLED_SERIES):
            continue
        # Match known game series or anything with "GAME" / "FIGHT" / "MATCH" in ticker
        if any(ticker.startswith(p) for p in SPORTS_GAME_SERIES):
            game_tickers.append(ticker)
        elif s.get("category", "") == "Sports" and any(
            kw in ticker.upper() for kw in ["GAME", "FIGHT", "MATCH", "BOUT"]
        ):
            game_tickers.append(ticker)
    return game_tickers


def is_game_nearly_over(market: dict, max_minutes: float = MAX_MINUTES_TO_EXPIRY) -> bool:
    """
    Check if a game is in its final stretch.

    Uses expected_expiration_time (when Kalshi expects the game to end).
    We only buy when the game is within max_minutes of ending,
    meaning we're in the last quarter/period/set where the outcome
    is essentially locked in.
    """
    exp_str = market.get("expected_expiration_time", "")
    if not exp_str:
        return False

    try:
        exp_time = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False

    now = datetime.now(timezone.utc)
    time_until_expiry = exp_time - now

    # Game must expire in the future (not settled yet)
    # AND within our tight window (game is nearly over)
    return timedelta(0) < time_until_expiry <= timedelta(minutes=max_minutes)


def has_liquidity(market: dict) -> bool:
    """Check that the market has enough volume/liquidity to trade."""
    min_vol = get_config_int("min_volume") or MIN_VOLUME
    prices = _parse_market_prices(market)
    volume = prices["volume"]
    yes_bid = prices["yes_bid"]
    return volume >= min_vol and yes_bid > 0


async def place_bet(
    client: KalshiClient,
    opp: dict,
    max_cost_cents: int,
    dry_run: bool = True,
) -> Optional[dict]:
    yes_price = opp["yes_ask"]
    count = max_cost_cents // yes_price
    if count < 1:
        log.info(f"  Cannot afford any contracts at {yes_price}c (budget: {max_cost_cents}c)")
        return None

    profit_per_contract = 100 - yes_price
    total_profit_if_win = count * profit_per_contract
    total_cost = count * yes_price

    log.info(
        f"  Order: BUY {count}x YES @ {yes_price}c = ${total_cost / 100:.2f} cost, "
        f"${total_profit_if_win / 100:.2f} potential profit | "
        f"ESPN: P{opp.get('espn_period', '')} {opp.get('espn_clock', '')}"
    )

    session = get_session()
    trade = Trade(
        ticker=opp["ticker"],
        event_ticker=opp["event_ticker"],
        title=opp["title"],
        side="yes",
        action="buy",
        count=count,
        yes_price=yes_price,
        cost_cents=total_cost,
        potential_profit_cents=total_profit_if_win,
        dry_run=dry_run,
        # Stop-loss context
        sport_path=opp.get("sport_path"),
        entry_lead=opp.get("espn_lead"),
        series_ticker=opp.get("series_ticker"),
    )

    if dry_run:
        log.info("  [DRY RUN] Order not placed")
        trade.status = "dry_run"
        session.add(trade)
        session.commit()
        session.close()
        return {"dry_run": True, "count": count, "yes_price": yes_price}

    # Write "pending" trade to DB BEFORE calling Kalshi API.
    # This prevents ghost orders: if we crash after Kalshi accepts the order
    # but before DB commit, the pending record ensures we know about it.
    trade.status = "pending"
    session.add(trade)
    session.commit()

    try:
        result = await client.create_order(
            ticker=opp["ticker"],
            side="yes",
            action="buy",
            count=count,
            yes_price=yes_price,
        )
        log.info(f"  Order placed: {result}")
        order = result.get("order", {})
        trade.order_id = order.get("order_id", "")
        trade.status = "placed"

        # Track partial fills — Kalshi may not fill the full count
        remaining = order.get("remaining_count", 0)
        if remaining and remaining > 0:
            filled_count = count - remaining
            if filled_count > 0:
                log.warning(
                    f"  PARTIAL FILL: requested {count}, got {filled_count} "
                    f"({remaining} remaining)"
                )
                trade.count = filled_count
                trade.cost_cents = filled_count * yes_price
                trade.potential_profit_cents = filled_count * (100 - yes_price)
            else:
                log.warning(f"  ORDER RESTING: 0 filled, {remaining} remaining")
                trade.status = "resting"

        session.commit()
        session.close()
        return result
    except Exception as e:
        log.error(f"  Order failed: {e}")
        trade.status = "error"
        trade.error = str(e)
        session.commit()
        session.close()
        return None


STOP_LOSS_DANGER_LEADS = {
    "basketball": 4,   # One possession away from tying
    "football": 6,     # One-score game
    "hockey": 0,       # Tied or trailing
    "baseball": 1,     # One swing ties it
    "soccer": 0,       # Tied or trailing
    "lacrosse": 2,     # Close game
    "australian-football": 12,  # Two goals
}


def _find_game_for_trade(trade, espn_caches: dict) -> object | None:
    """Find the ESPN/SofaScore game state for an open trade.

    Searches all cached games (final-minutes + final-period) for a match
    by comparing team names in the trade title against game teams.
    """
    title_upper = (trade.title or "").upper()
    if not title_upper:
        return None
    for games in espn_caches.values():
        for game in games:
            home = game.home_team.upper()
            away = game.away_team.upper()
            if (len(home) >= 3 and home in title_upper) or (len(away) >= 3 and away in title_upper):
                return game
    return None


async def check_stop_losses(client: KalshiClient, espn_caches: dict):
    """Game-state-aware stop-loss: only sell when BOTH price AND game state confirm danger.

    This prevents selling into thin order books where yes_bid is low but the team
    is still winning comfortably (e.g. Kennesaw State: bid=1c but team was ahead).
    """

    stop_price = get_config_int("stop_loss_price")
    if not stop_price:
        return  # Stop-loss disabled (no config set)

    session = get_session()
    open_trades = (
        session.query(Trade)
        .filter(Trade.status.in_(("placed", "filled", "stop_failed")), Trade.dry_run == False)
        .all()
    )

    for trade in open_trades:
        # Skip if already exhausted retries
        attempts = _stop_loss_attempts.get(trade.ticker, 0)
        if attempts >= 3:
            if trade.status != "stop_failed":
                trade.status = "stop_failed"
                scan_debug["last_errors"].append(
                    f"STOP-LOSS FAILED after 3 attempts: {trade.ticker}"
                )
                log.error(f"  STOP-LOSS FAILED after 3 attempts: {trade.ticker}")
            continue

        # Get current YES bid from WebSocket data
        live = market_prices.get(trade.ticker, {})
        yes_bid = live.get("yes_bid", 0)
        updated_at = live.get("updated_at", 0)

        # If WS data is stale (>60s), fetch fresh price from REST API
        if not yes_bid or (time.time() - updated_at > 60):
            try:
                fresh = await client.get_market(trade.ticker)
                fresh_parsed = _parse_market_prices(fresh)
                yes_bid = fresh_parsed.get("yes_bid", 0)
                if yes_bid:
                    log.warning(
                        f"  Stop-loss using REST fallback for {trade.ticker} "
                        f"(WS data {int(time.time() - updated_at)}s old) bid={yes_bid}c"
                    )
            except Exception as e:
                log.error(f"  Stop-loss REST fallback failed for {trade.ticker}: {e}")

        if not yes_bid:
            continue  # No price data at all, skip

        if yes_bid > stop_price:
            continue  # Price is fine, no action needed

        # ── Price signal triggered (bid <= stop_price) ──
        # Now check game state before selling

        game = _find_game_for_trade(trade, espn_caches)
        if game:
            sport_prefix = game.sport_path.split("/")[0]
            danger_lead = STOP_LOSS_DANGER_LEADS.get(sport_prefix, 0)

            if game.score_diff > danger_lead:
                # Lead is still safe — this is a thin order book, not a real crash
                log.info(
                    f"  STOP-LOSS HOLD: {trade.ticker} bid={yes_bid}c but "
                    f"lead={game.score_diff} > danger={danger_lead} — thin book, holding"
                )
                continue
        else:
            # No game data available — never sell blind
            log.warning(
                f"  STOP-LOSS SKIP: {trade.ticker} bid={yes_bid}c but no ESPN data — "
                f"holding (won't sell blind)"
            )
            continue

        # ── Both signals confirm: price low AND game state dangerous ──
        # Verify price via REST API before selling (WS might be stale/wrong)
        try:
            fresh_market = await client.get_market(trade.ticker)
            fresh_bid = _parse_market_prices(fresh_market).get("yes_bid", 0)
            if fresh_bid and fresh_bid > stop_price:
                log.info(
                    f"  STOP-LOSS ABORT: WS bid={yes_bid}c but REST bid={fresh_bid}c > "
                    f"{stop_price}c — WS was stale"
                )
                continue
            if fresh_bid:
                yes_bid = fresh_bid  # Use fresh price for sell
        except Exception as e:
            log.error(f"  Stop-loss REST verify failed for {trade.ticker}: {e}")
            continue  # Can't verify → don't sell

        log.warning(
            f"  STOP-LOSS TRIGGERED: {trade.ticker} bid={yes_bid}c <= {stop_price}c "
            f"game_lead={game.score_diff} (danger={danger_lead}) "
            f"(bought @ {trade.yes_price}c, attempt {attempts + 1}/3)"
        )
        # Sell aggressively: 5c below bid for guaranteed fill
        sell_price = max(1, yes_bid - 5)
        try:
            result = await client.create_order(
                ticker=trade.ticker,
                side="yes",
                action="sell",
                count=trade.count,
                yes_price=sell_price,
            )
            loss_cents = (trade.yes_price - sell_price) * trade.count
            trade.status = "stopped_out"
            trade.pnl_cents = -loss_cents
            # Clear retry counter on success
            _stop_loss_attempts.pop(trade.ticker, None)
            log.warning(
                f"  STOP-LOSS SOLD: {trade.ticker} {trade.count}x @ {sell_price}c | "
                f"Loss: ${loss_cents / 100:.2f} (saved ${(trade.cost_cents - loss_cents) / 100:.2f} vs full loss)"
            )
        except Exception as e:
            _stop_loss_attempts[trade.ticker] = attempts + 1
            log.error(
                f"  Stop-loss sell failed for {trade.ticker} "
                f"(attempt {attempts + 1}/3): {e}"
            )
            scan_debug["last_errors"].append(
                f"STOP-LOSS SELL FAILED: {trade.ticker} attempt {attempts + 1}/3 — {e}"
            )

    session.commit()
    session.close()


async def check_settlements(client: KalshiClient):
    """Check open trades for settlement and update P&L."""
    session = get_session()
    open_trades = (
        session.query(Trade)
        .filter(Trade.status.in_(("pending", "placed", "filled")), Trade.dry_run == False)
        .all()
    )

    for trade in open_trades:
        try:
            market = await client.get_market(trade.ticker)
            status = market.get("status", "")
            result = market.get("result", "")

            if status in ("finalized", "settled"):
                if result == trade.side:
                    # Won: each contract pays $1, profit = (100 - price) * count
                    trade.status = "settled_win"
                    trade.pnl_cents = trade.potential_profit_cents
                    log.info(
                        f"  WIN: {trade.ticker} settled {result} | "
                        f"P&L: +${trade.pnl_cents / 100:.2f}"
                    )
                else:
                    # Lost: lose the cost
                    trade.status = "settled_loss"
                    trade.pnl_cents = -trade.cost_cents
                    log.info(
                        f"  LOSS: {trade.ticker} settled {result} | "
                        f"P&L: -${trade.cost_cents / 100:.2f}"
                    )
        except Exception as e:
            log.warning(f"  Failed to check {trade.ticker}: {e}")

    session.commit()
    session.close()


# Max daily loss before scanner stops trading (cents)
MAX_DAILY_LOSS_CENTS = 1000  # $10 default, configurable via "max_daily_loss" config


def _daily_loss_exceeded() -> bool:
    """Check if we've lost more than the daily limit today."""
    max_loss = get_config_int("max_daily_loss") or MAX_DAILY_LOSS_CENTS
    session = get_session()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    losses = (
        session.query(Trade)
        .filter(
            Trade.status.in_(("settled_loss", "stopped_out")),
            Trade.placed_at >= today,
            Trade.dry_run == False,
        )
        .all()
    )
    total_loss = sum(abs(t.pnl_cents or 0) for t in losses)
    session.close()
    if total_loss >= max_loss:
        log.warning(
            f"DAILY LOSS LIMIT: ${total_loss/100:.2f} lost today "
            f"(limit: ${max_loss/100:.2f}) — pausing new bets"
        )
        return True
    return False


async def record_balance(client: KalshiClient):
    try:
        balance = await client.get_balance()
        session = get_session()
        snap = BalanceSnapshot(
            balance_cents=balance.get("balance", 0),
            portfolio_value_cents=balance.get("portfolio_value", 0),
        )
        session.add(snap)
        session.commit()
        session.close()
        bal = balance.get("balance", 0) / 100
        port = balance.get("portfolio_value", 0) / 100
        log.info(f"Balance: ${bal:.2f}, Portfolio: ${port:.2f}")
    except Exception as e:
        log.warning(f"Failed to record balance: {e}")


async def sync_positions(client: KalshiClient):
    """Sync DB trades with actual Kalshi positions. Detects manual closes/fills."""
    try:
        session = get_session()
        open_trades = (
            session.query(Trade)
            .filter(Trade.status.in_(("pending", "placed", "filled", "resting")), Trade.dry_run == False)
            .all()
        )
        if not open_trades:
            session.close()
            return

        # Get all positions from Kalshi
        positions_data = await client.get_positions(limit=200, settlement_status="unsettled")
        kalshi_tickers = {}
        for pos in positions_data.get("market_positions", []):
            ticker = pos.get("market_ticker", "")
            qty = pos.get("total_traded", 0) - pos.get("total_cost", 0)
            # yes position = positive resting_orders_count or position
            yes_count = pos.get("position", 0)
            kalshi_tickers[ticker] = yes_count

        # Also check settled positions
        settled_data = await client.get_positions(limit=200, settlement_status="settled")
        settled_tickers = {}
        for pos in settled_data.get("market_positions", []):
            ticker = pos.get("market_ticker", "")
            settled_tickers[ticker] = pos

        for trade in open_trades:
            if trade.ticker in settled_tickers:
                # Kalshi settled this — check if win or loss
                pos = settled_tickers[trade.ticker]
                payout = pos.get("settlement_payout", 0)
                if payout > 0:
                    trade.status = "settled_win"
                    trade.pnl_cents = trade.potential_profit_cents
                    log.info(f"  SYNC WIN: {trade.ticker} (settled on Kalshi)")
                else:
                    trade.status = "settled_loss"
                    trade.pnl_cents = -trade.cost_cents
                    log.info(f"  SYNC LOSS: {trade.ticker} (settled on Kalshi)")
            elif trade.ticker not in kalshi_tickers or kalshi_tickers.get(trade.ticker, 0) == 0:
                # No position on Kalshi — but only mark as manual_close if trade
                # is old enough (>10 min) to avoid race with Kalshi API lag
                age = (datetime.now(timezone.utc) - trade.placed_at).total_seconds() if trade.placed_at else 0
                if age > 600:  # 10 minutes grace period
                    trade.status = "manual_close"
                    trade.pnl_cents = 0  # Unknown P&L from manual close
                    log.info(f"  SYNC: {trade.ticker} manually closed (no Kalshi position)")
                else:
                    log.info(f"  SYNC: {trade.ticker} not on Kalshi yet, waiting ({int(age)}s old)")

        session.commit()
        session.close()
    except Exception as e:
        log.warning(f"Failed to sync positions: {e}")


async def scan_kalshi_with_espn(
    client: KalshiClient,
    espn_final: dict,
    min_yes_price: int,
    max_bet_cents: int,
    dry_run: bool,
    espn_final_period: dict | None = None,
):
    """Scan Kalshi markets against cached ESPN game state and place bets."""
    opportunities = []

    if not espn_final and not espn_final_period:
        log.info("No ESPN games in final minutes — skipping Kalshi scan")
        return

    # Scan Kalshi markets against ESPN games
    for series_ticker, espn_games in espn_final.items():
        try:
            cursor = None
            while True:
                data = await client.get_events(
                    status="open",
                    series_ticker=series_ticker,
                    with_nested_markets=True,
                    cursor=cursor,
                )
                events = data.get("events", [])
                if not events:
                    break

                for event in events:
                    event_ticker = event.get("event_ticker", "")
                    title = event.get("title", "")
                    markets = event.get("markets", [])

                    # ── Pre-parse ALL markets to find highest-priced team ──
                    parsed_markets = []
                    for market in markets:
                        status = market.get("status", "")
                        if status not in ("active", "open"):
                            continue
                        ticker = market.get("ticker", "")
                        parsed = _parse_market_prices(market)
                        live = market_prices.get(ticker, {})
                        if live.get("yes_bid"):
                            parsed["yes_bid"] = live["yes_bid"]
                        if live.get("yes_ask"):
                            parsed["yes_ask"] = live["yes_ask"]
                        if live.get("volume"):
                            parsed["volume"] = live["volume"]
                        m = dict(market)
                        m["yes_bid"] = parsed["yes_bid"]
                        m["yes_ask"] = parsed["yes_ask"]
                        m["volume"] = parsed["volume"]
                        m["_ticker"] = ticker
                        m["_parsed"] = parsed
                        parsed_markets.append(m)

                    # Find which market Kalshi thinks is the favorite (highest YES bid)
                    kalshi_favorite_suffix = ""
                    kalshi_best_bid = 0
                    for m in parsed_markets:
                        suffix = m["_ticker"].split("-")[-1].upper()
                        if suffix == "TIE":
                            continue
                        bid = m["_parsed"]["yes_bid"] or 0
                        if bid > kalshi_best_bid:
                            kalshi_best_bid = bid
                            kalshi_favorite_suffix = suffix

                    for market in parsed_markets:
                        ticker = market["_ticker"]
                        parsed = market["_parsed"]

                        if not has_liquidity(market):
                            continue

                        yes_bid = parsed["yes_bid"]
                        yes_ask = parsed["yes_ask"]

                        # Price must be within tradeable range
                        if not (yes_ask and yes_ask >= min_yes_price and yes_ask <= 99):
                            continue

                        espn_game = match_kalshi_to_espn(ticker, title, espn_games)
                        if not espn_game:
                            continue

                        # ── SAFETY: Verify market is for the LEADING team ──
                        market_team_suffix = ticker.split("-")[-1].upper()
                        yes_sub = market.get("yes_sub_title", "").upper()

                        if market_team_suffix == "TIE":
                            continue

                        leading = espn_game.leading_team.upper()
                        if leading == "TIED":
                            continue

                        # Check 1: Market team must match ESPN leading team
                        leading_full = ""
                        if espn_game.home_score > espn_game.away_score:
                            leading_full = getattr(espn_game, "home_full_name", "").upper()
                        elif espn_game.away_score > espn_game.home_score:
                            leading_full = getattr(espn_game, "away_full_name", "").upper()

                        team_is_leader = (
                            market_team_suffix == leading
                            or leading in yes_sub
                            or (leading_full and len(leading_full) >= 3 and leading_full in yes_sub)
                        )
                        if not team_is_leader:
                            log.debug(
                                f"  SKIP wrong team: {ticker} market={market_team_suffix} "
                                f"but leader={leading} ({leading_full})"
                            )
                            continue

                        # Check 2: Cross-validate — Kalshi's favorite must be OUR leader
                        # If Kalshi thinks a different team is winning, our data may be wrong
                        if kalshi_favorite_suffix and kalshi_favorite_suffix != market_team_suffix:
                            log.warning(
                                f"  DATA MISMATCH: ESPN says {leading} leads, "
                                f"but Kalshi favorite is {kalshi_favorite_suffix} "
                                f"(bid={kalshi_best_bid}c). SKIPPING {ticker} for safety."
                            )
                            scan_debug["last_errors"].append(
                                f"DATA MISMATCH: {ticker} ESPN={leading} vs Kalshi={kalshi_favorite_suffix}"
                            )
                            continue

                        db_lead = get_config_int(f"lead:{espn_game.sport_path}")
                        fallback = MIN_SCORE_LEAD.get(espn_game.sport_path, 5)
                        min_lead = db_lead if db_lead else fallback
                        # OT: require 2x lead — desperation play makes comebacks more likely
                        # Football/hockey OT is too volatile (sudden death), skip entirely
                        if espn_game.is_overtime:
                            if "football" in espn_game.sport_path or "hockey" in espn_game.sport_path:
                                continue
                            min_lead = int(min_lead * 2.0 + 0.5)
                        max_yes = get_config_int("max_yes_price") or 99
                        meets_price = min_yes_price <= yes_ask <= max_yes
                        meets_lead = espn_game.score_diff >= min_lead

                        if meets_price and meets_lead:
                            # Full opportunity — meets all filters
                            spread = 100 - yes_ask
                            opportunities.append(
                                {
                                    "ticker": ticker,
                                    "event_ticker": event_ticker,
                                    "title": title,
                                    "yes_sub_title": market.get("yes_sub_title", ""),
                                    "yes_bid": yes_bid,
                                    "yes_ask": yes_ask,
                                    "spread": spread,
                                    "volume": market.get("volume", 0),
                                    "close_time": market.get("close_time", ""),
                                    "expected_expiration": market.get(
                                        "expected_expiration_time", ""
                                    ),
                                    "series_ticker": series_ticker,
                                    "sport_path": espn_game.sport_path,
                                    "espn_period": espn_game.period,
                                    "espn_clock": espn_game.display_clock,
                                    "espn_clock_seconds": espn_game.clock_seconds,
                                    "espn_home": espn_game.home_team,
                                    "espn_away": espn_game.away_team,
                                    "espn_home_score": espn_game.home_score,
                                    "espn_away_score": espn_game.away_score,
                                    "espn_score": f"{espn_game.away_score}-{espn_game.home_score}",
                                    "espn_lead": espn_game.score_diff,
                                    "min_lead": min_lead,
                                }
                            )
                        else:
                            continue  # doesn't meet price + lead filters

                cursor = data.get("cursor", "")
                if not cursor:
                    break

        except Exception as e:
            log.warning(f"Error scanning series {series_ticker}: {e}")
            continue

    # Annotate each opportunity with its priority tier
    for opp in opportunities:
        opp["_tier"] = _get_sport_tier(opp.get("sport_path", ""))

    # Tier-aware volume gate: Tier 3 requires much higher liquidity
    min_vol = get_config_int("min_volume") or MIN_VOLUME
    min_vol_t3 = max(min_vol, 2000)
    filtered_opps = []
    for opp in opportunities:
        vol = opp.get("volume", 0)
        if opp["_tier"] == 3 and vol < min_vol_t3:
            log.info(f"  VOLUME GATE: {opp['ticker']} T3 vol={vol} < {min_vol_t3}")
            continue
        filtered_opps.append(opp)
    opportunities = filtered_opps

    # Sort: Tier 1 first, then 2, then 3. Within tier, best spread/lead first.
    opportunities.sort(key=lambda x: (x["_tier"], -x["spread"], -x["espn_lead"]))

    # Record scan and process opportunities
    session = get_session()
    scan = Scan(opportunities_found=len(opportunities))
    session.add(scan)
    session.commit()
    scan_id = scan.id

    # Update debug state
    scan_debug["espn_cache_keys"] = list(espn_final.keys())
    scan_debug["last_opps"] = [
        {"ticker": o["ticker"], "ask": o["yes_ask"], "lead": o.get("espn_lead"), "sport": o.get("sport_path")}
        for o in opportunities[:5]
    ]
    scan_debug["last_skips"] = []
    scan_debug["last_errors"] = []

    if not opportunities:
        log.info("No Kalshi opportunities matched ESPN games")
    else:
        try:
            # Include manual_close, resting, and pending to prevent re-betting same event
            open_statuses = ("pending", "placed", "filled", "dry_run", "resting", "manual_close", "stopped_out")
            open_trades = (
                session.query(Trade)
                .filter(Trade.status.in_(open_statuses), Trade.dry_run == dry_run)
                .all()
            )
            # For position count, only count truly open positions (not manual_close)
            open_event_tickers = {t.event_ticker for t in open_trades}
            open_count = len([t for t in open_trades if t.status not in ("manual_close",)])
        except Exception as e:
            log.error(f"Error querying open trades: {e}")
            scan_debug["last_errors"].append(f"TRADE_QUERY_ERROR: {e}")
            open_event_tickers = set()
            open_count = 0

        max_pos = get_config_int("max_positions") or 20

        # ── Tier-aware slot allocation ──
        tier1_reserved = get_config_int("tier1_reserved_slots") or 2
        tier3_max = get_config_int("tier3_max_slots") or 1

        # Count open positions by tier
        open_tier_counts: dict[int, int] = {1: 0, 2: 0, 3: 0}
        for t in open_trades:
            if t.status not in ("manual_close",):
                t_tier = _get_sport_tier(t.sport_path) if t.sport_path else 2
                open_tier_counts[t_tier] = open_tier_counts.get(t_tier, 0) + 1

        # Check if higher-tier opportunities exist in this scan
        has_t1_opps = any(o.get("_tier") == 1 for o in opportunities
                         if o["event_ticker"] not in open_event_tickers)
        has_t2_opps = any(o.get("_tier") == 2 for o in opportunities
                         if o["event_ticker"] not in open_event_tickers)

        log.info(
            f"Found {len(opportunities)} opportunities on live games "
            f"({open_count}/{max_pos} open positions, "
            f"T1:{open_tier_counts[1]} T2:{open_tier_counts[2]} T3:{open_tier_counts[3]}):"
        )
        for opp in opportunities:
          try:
            opp_tier = opp.get("_tier", 2)
            log.info(
                f"  [T{opp_tier}] {opp['ticker']} | {opp.get('yes_sub_title', '')} | "
                f"Yes Ask: {opp['yes_ask']}c | Spread: {opp.get('spread', '?')}c | "
                f"ESPN: P{opp.get('espn_period', '?')} {opp.get('espn_clock', '?')} "
                f"{opp.get('espn_away', '?')}@{opp.get('espn_home', '?')} {opp.get('espn_score', '?')} | "
                f"Vol: {opp.get('volume', 0)}"
            )

            try:
                db_opp = Opportunity(
                    scan_id=scan_id,
                    ticker=opp["ticker"],
                    event_ticker=opp["event_ticker"],
                    series_ticker=opp.get("series_ticker", ""),
                    title=opp.get("title", ""),
                    yes_sub_title=opp.get("yes_sub_title", ""),
                    yes_bid=opp.get("yes_bid", 0),
                    yes_ask=opp["yes_ask"],
                    spread=opp.get("spread", 0),
                    volume=opp.get("volume", 0),
                    close_time=opp.get("close_time", ""),
                    sport_path=opp.get("sport_path"),
                    espn_period=opp.get("espn_period"),
                    espn_clock=opp.get("espn_clock"),
                    espn_home=opp.get("espn_home"),
                    espn_away=opp.get("espn_away"),
                    espn_home_score=opp.get("espn_home_score"),
                    espn_away_score=opp.get("espn_away_score"),
                    espn_score_diff=opp.get("espn_lead"),
                )
                session.add(db_opp)
            except Exception as e:
                log.warning(f"  Could not save opportunity to DB: {e}")

            if opp["event_ticker"] in open_event_tickers:
                skip = f"SKIP: already have position on {opp['event_ticker']}"
                log.info(f"  {skip}")
                scan_debug["last_skips"].append(skip)
                continue

            if open_count >= max_pos:
                skip = f"SKIP: at max {max_pos} open positions"
                log.info(f"  {skip}")
                scan_debug["last_skips"].append(skip)
                continue

            # ── Tier gating ──
            # 1. Tier 3 cap: never exceed tier3_max_slots
            if opp_tier == 3 and open_tier_counts.get(3, 0) >= tier3_max:
                skip = f"SKIP: Tier 3 at max {tier3_max} slots"
                log.info(f"  {skip}")
                scan_debug["last_skips"].append(skip)
                continue

            # 2. Tier 3 blocked if higher-tier opportunities available
            if opp_tier == 3 and (has_t1_opps or has_t2_opps):
                skip = "SKIP: Tier 3 blocked — Tier 1/2 opportunities available"
                log.info(f"  {skip}")
                scan_debug["last_skips"].append(skip)
                continue

            # 3. Reserved slots: keep last N slots for Tier 1 only
            remaining_slots = max_pos - open_count
            if opp_tier > 1 and remaining_slots <= tier1_reserved:
                skip = f"SKIP: {remaining_slots} slots left, {tier1_reserved} reserved for Tier 1"
                log.info(f"  {skip}")
                scan_debug["last_skips"].append(skip)
                continue

            if not dry_run and _daily_loss_exceeded():
                skip = "SKIP: daily loss limit reached"
                log.info(f"  {skip}")
                scan_debug["last_skips"].append(skip)
                continue

            scan_debug["last_skips"].append(f"ATTEMPTING: {opp['ticker']} @ {opp['yes_ask']}c | dry_run={dry_run} | max_cost={max_bet_cents}c | open={open_count}/{max_pos}")
            log.info(f"  ATTEMPTING BET: dry_run={dry_run}, max_cost={max_bet_cents}c")
            try:
                result = await place_bet(client, opp, max_cost_cents=max_bet_cents, dry_run=dry_run)
                if result:
                    open_event_tickers.add(opp["event_ticker"])
                    open_count += 1
                    open_tier_counts[opp_tier] = open_tier_counts.get(opp_tier, 0) + 1
                    scan_debug["last_skips"].append(f"BET PLACED: [T{opp_tier}] {opp['ticker']} @ {opp['yes_ask']}c | result={result}")
                else:
                    scan_debug["last_skips"].append(f"BET RETURNED NONE: {opp['ticker']} @ {opp['yes_ask']}c")
            except Exception as e:
                import traceback
                err = f"BET ERROR: {opp['ticker']} — {e}\n{traceback.format_exc()}"
                log.error(err)
                scan_debug["last_errors"].append(err)
          except Exception as e:
            import traceback
            err = f"OPP_LOOP_ERROR: {opp.get('ticker', '?')} — {e}\n{traceback.format_exc()}"
            log.error(err)
            scan_debug["last_errors"].append(err)
            continue  # Don't let one bad opp kill the whole batch

    try:
        session.commit()
    except Exception as e:
        log.error(f"Session commit error: {e}")
        scan_debug["last_errors"].append(f"COMMIT_ERROR: {e}")
        session.rollback()

    session.close()



async def backup_db():
    """Copy SQLite DB to S3 if bucket is configured."""
    bucket = os.getenv("DB_BACKUP_BUCKET")
    if not bucket:
        return
    db_url = os.getenv("DATABASE_URL", "")
    db_path = db_url.replace("sqlite:///", "") if db_url.startswith("sqlite:///") else None
    if not db_path or not os.path.exists(db_path):
        return
    try:
        import shutil
        from datetime import datetime, timezone

        import boto3

        # Use SQLite backup API (safe for WAL mode / concurrent access)
        import sqlite3

        tmp = db_path + ".backup"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.close()
        src.close()

        s3 = boto3.client("s3")
        now = datetime.now(timezone.utc)
        key = f"backups/{now.strftime('%Y-%m-%d/%H%M')}-predictions.db"
        s3.upload_file(tmp, bucket, key)

        # Also keep a "latest" copy for easy restore
        s3.upload_file(tmp, bucket, "backups/latest.db")
        os.remove(tmp)
        log.info(f"DB backed up to s3://{bucket}/{key}")
    except Exception as e:
        log.warning(f"DB backup failed: {e}")


async def run_scanner(
    min_yes_price: int = 88,
    max_bet_cents: int = 500,
    poll_interval: int = 30,
    dry_run: bool = True,
):
    init_db()
    client = load_client()
    await record_balance(client)

    # Reconcile any "pending" trades from previous crashes.
    # These are trades we wrote to DB before sending to Kalshi.
    # If Kalshi accepted the order, it exists; if not, mark as error.
    session = get_session()
    pending_trades = session.query(Trade).filter(Trade.status == "pending", Trade.dry_run == False).all()
    for trade in pending_trades:
        log.warning(f"RECONCILE: Found pending trade from crash: {trade.ticker}")
        try:
            # Check if the market has settled or if we have a position
            market = await client.get_market(trade.ticker)
            mkt_status = market.get("status", "")
            mkt_result = market.get("result", "")
            if mkt_status in ("finalized", "settled"):
                if mkt_result == trade.side:
                    trade.status = "settled_win"
                    trade.pnl_cents = trade.potential_profit_cents
                else:
                    trade.status = "settled_loss"
                    trade.pnl_cents = -trade.cost_cents
                log.info(f"  Reconciled as {trade.status} (market already settled)")
            else:
                # Market still open — assume order went through (safer than assuming it didn't)
                trade.status = "placed"
                log.info(f"  Marked as placed (market still open, assuming order succeeded)")
        except Exception as e:
            log.error(f"  Reconcile failed for {trade.ticker}: {e} — marking as error")
            trade.status = "error"
            trade.error = f"reconcile_failed: {e}"
    if pending_trades:
        session.commit()
    session.close()

    espn_interval = 10  # Refresh ESPN game state every 10s

    # Shared state protected by locks
    espn_cache: dict = {}
    espn_final_period_cache: dict = {}
    espn_last_updated: float = 0.0  # monotonic timestamp of last ESPN refresh
    espn_lock = asyncio.Lock()

    # Track live market prices from WebSocket ticker updates (module-level for what-if access)
    global market_prices
    market_prices = {}  # ticker -> {yes_bid, yes_ask, volume}

    # Track which market tickers we're subscribed to
    subscribed_tickers: set[str] = set()
    ticker_sub_sid: int | None = None
    lifecycle_sub_sid: int | None = None

    # Daily starting balance — set once per day, used for percentage-based bet sizing
    daily_balance: dict = {"date": None, "balance": 0}

    ws = KalshiWebSocket(client)

    def on_ws_reconnect():
        """Called when WS reconnects — clear subscription state so next scan re-subscribes."""
        nonlocal ticker_sub_sid, lifecycle_sub_sid
        log.warning("WS reconnected — clearing subscription state for re-subscribe")
        subscribed_tickers.clear()
        ticker_sub_sid = None
        lifecycle_sub_sid = None

    ws._on_reconnect = on_ws_reconnect

    def on_ticker(msg: dict):
        """Handle real-time price updates from WebSocket."""
    

        data = msg.get("msg", {})
        ticker = data.get("market_ticker", "")
        if ticker:
            # WS may use dollar fields or legacy integer fields
            parsed = _parse_market_prices(data)
            if parsed["yes_bid"] or parsed["yes_ask"]:
                parsed["updated_at"] = time.time()
                market_prices[ticker] = parsed
            else:
                # Fallback: try raw integer fields from WS message
                # Only update if we got real price data — don't write 0-bid with fresh timestamp
                raw_bid = data.get("yes_bid", 0) or 0
                raw_ask = data.get("yes_ask", 0) or 0
                if raw_bid or raw_ask:
                    market_prices[ticker] = {
                        "yes_bid": raw_bid,
                        "yes_ask": raw_ask,
                        "volume": data.get("volume", 0) or 0,
                        "updated_at": time.time(),
                    }

    async def on_lifecycle(msg: dict):
        """Handle market lifecycle events (settlement)."""
        data = msg.get("msg", {})
        ticker = data.get("market_ticker", "")
        new_status = data.get("market_status", "")
        result = data.get("result", "")

        if new_status in ("finalized", "settled") and ticker:
            log.info(f"WS lifecycle: {ticker} -> {new_status} result={result}")
            # Update real trades
            session = get_session()
            open_trades = (
                session.query(Trade)
                .filter(
                    Trade.ticker == ticker,
                    Trade.status.in_(("pending", "placed", "filled")),
                    Trade.dry_run == False,
                )
                .all()
            )
            for trade in open_trades:
                if result == trade.side:
                    trade.status = "settled_win"
                    trade.pnl_cents = trade.potential_profit_cents
                    log.info(f"  WIN: {trade.ticker} | P&L: +${trade.pnl_cents / 100:.2f}")
                else:
                    trade.status = "settled_loss"
                    trade.pnl_cents = -trade.cost_cents
                    log.info(f"  LOSS: {trade.ticker} | P&L: -${trade.cost_cents / 100:.2f}")

            session.commit()
            session.close()
            await record_balance(client)

    ws.on("ticker", on_ticker)
    ws.on("market_lifecycle_v2", on_lifecycle)

    async def espn_loop():
        """Refresh ESPN + SofaScore final-minutes games every 10s."""
        nonlocal espn_cache, espn_final_period_cache, espn_last_updated
        while True:
            try:
                log.info("ESPN: refreshing live game state...")
                fresh, fresh_fp = await get_categorized_games()

                # Add blowout tier ESPN games to final_minutes
                # (games with huge leads that haven't reached normal timing yet)
                for series, fp_games in fresh_fp.items():
                    for g in fp_games:
                        if series in fresh and g in fresh[series]:
                            continue  # already in final minutes
                        min_lead = MIN_SCORE_LEAD.get(g.sport_path, 5)
                        db_lead = get_config_int(f"lead:{g.sport_path}")
                        if db_lead:
                            min_lead = db_lead
                        if meets_blowout_tier(g, min_lead):
                            if series not in fresh:
                                fresh[series] = []
                            fresh[series].append(g)
                            log.info(f"  BLOWOUT: {g.away_team}@{g.home_team} {g.away_score}-{g.home_score} P{g.period} {g.display_clock} (lead={g.score_diff})")

                # Also fetch SofaScore for international leagues
                try:
                    ss_games = await get_all_sofascore_games()
                    if ss_games:
                        log.info(f"SofaScore: {sum(len(g) for g in ss_games.values())} live int'l games across {list(ss_games.keys())}")
                    for series, games in ss_games.items():
                        # Check which games are in final minutes or meet blowout tier
                        fm_games = []
                        fp_games = []
                        for g in games:
                            final_period = SOFASCORE_FINAL_PERIOD.get(g.sport_path, 4)
                            if g.period >= final_period:
                                fp_games.append(g)
                                # Check normal timing
                                normal_fm = False
                                if "soccer" in g.sport_path:
                                    final_secs = get_config_int(f"final_seconds:{g.sport_path}") or 4500
                                    normal_fm = g.clock_seconds >= final_secs
                                else:
                                    final_secs = get_config_int(f"final_seconds:{g.sport_path}") or 300
                                    normal_fm = g.clock_seconds <= final_secs
                                # Check blowout tier (bigger lead = earlier entry)
                                min_lead = SOFASCORE_SCORE_LEAD.get(g.sport_path, MIN_SCORE_LEAD.get(g.sport_path, 5))
                                is_blowout = meets_blowout_tier(g, min_lead)
                                if normal_fm or is_blowout:
                                    fm_games.append(g)
                                    if is_blowout and not normal_fm:
                                        log.info(f"  BLOWOUT: {g.away_team}@{g.home_team} {g.away_score}-{g.home_score} P{g.period} {g.display_clock} (lead={g.score_diff}, need={int(min_lead * (_get_blowout_tier(g.sport_path) or (1,0,0))[0])})")
                        if fm_games:
                            fresh[series] = fm_games
                        if fp_games:
                            fresh_fp[series] = fp_games
                except Exception as e:
                    log.warning(f"SofaScore refresh error: {e}")

            
                async with espn_lock:
                    espn_cache = fresh
                    espn_final_period_cache = fresh_fp
                    espn_last_updated = time.monotonic()
                total = sum(len(g) for g in fresh.values())
                if total:
                    log.info(f"ESPN+SS: {total} games in final minutes across {list(fresh.keys())}")
                    for games in fresh.values():
                        for g in games:
                            source = "SS" if g.espn_id.startswith("ss-") else "ESPN"
                            log.info(
                                f"  {source}: {g.away_team} @ {g.home_team} | "
                                f"{g.away_score}-{g.home_score} | "
                                f"P{g.period} {g.display_clock} | "
                                f"Lead: {g.score_diff}pts by {g.leading_team}"
                            )
                else:
                    log.info("ESPN+SS: no games in final minutes")
            except Exception as e:
                log.warning(f"ESPN refresh error: {e}")
            await asyncio.sleep(espn_interval)

    async def kalshi_scan_loop():
        """Fetch Kalshi events, subscribe to new tickers, evaluate."""
        nonlocal ticker_sub_sid, lifecycle_sub_sid
        kalshi_interval = 5  # Discover new markets every 5s

        # Wait for first ESPN fetch + WS connect
        await asyncio.sleep(3)

        while True:
            try:
                log.info("=" * 60)
                # Re-read config each loop so changes take effect immediately
                cur_price = get_config_int("min_yes_price") or min_yes_price
                # Bet sizing: use percentage of DAILY STARTING balance if configured
                bet_pct = get_config_int("max_bet_pct")
                if bet_pct and bet_pct > 0:
                    try:
                        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        bal = await client.get_balance()
                        current_balance = bal.get("balance", 0)
                        # Lock in the daily starting balance once per day
                        if daily_balance["date"] != today:
                            daily_balance["date"] = today
                            daily_balance["balance"] = current_balance
                            log.info(f"Daily starting balance set: ${current_balance/100:.2f}")
                        base = daily_balance["balance"]
                        cur_bet = int(base * bet_pct / 100)
                        log.info(f"Bet sizing: {bet_pct}% of ${base/100:.2f} (day start) = ${cur_bet/100:.2f}")
                    except Exception:
                        cur_bet = get_config_int("max_bet_cents") or max_bet_cents
                else:
                    cur_bet = get_config_int("max_bet_cents") or max_bet_cents
                # Allow toggling dry_run from dashboard config
                from db import get_config as _gc
                dr_val = _gc("dry_run")
                if dr_val:
                    dry_run = dr_val.lower() == "true"
                log.info(f"Kalshi: scanning for Yes >= {cur_price}c...")
            
                async with espn_lock:
                    current_espn = dict(espn_cache)
                    current_espn_fp = dict(espn_final_period_cache)
                    espn_age = time.monotonic() - espn_last_updated if espn_last_updated else 999

                # Safety: skip betting if ESPN data is too stale (>30s)
                if espn_age > 30:
                    log.warning(f"ESPN data is {espn_age:.0f}s stale — skipping bet scan for safety")
                    scan_debug["last_errors"].append(f"ESPN_STALE: data is {espn_age:.0f}s old")
                    await asyncio.sleep(kalshi_interval)
                    continue

                # Discover all active market tickers from Kalshi API
                # Include both final-minutes and final-period series for what-if tracking
                # Rate limit: small delay between series to avoid Kalshi 429s
                all_series = set(current_espn.keys()) | set(current_espn_fp.keys())
                new_tickers: set[str] = set()
                for i, series_ticker in enumerate(all_series):
                    if i > 0:
                        await asyncio.sleep(0.25)  # ~4 req/s to stay under rate limit
                    try:
                        # Use get_markets for real price data (get_events nested data often has 0s)
                        cursor = None
                        while True:
                            data = await client.get_markets(
                                series_ticker=series_ticker,
                                status="open",
                                cursor=cursor,
                            )
                            for market in data.get("markets", []):
                                t = market.get("ticker", "")
                                if t and market.get("status") in ("active", "open"):
                                    new_tickers.add(t)
                                    # Seed/update prices from API (WS will override with real-time)
                                    existing = market_prices.get(t, {})
                                    if not existing.get("yes_bid") and not existing.get("yes_ask"):
                                        parsed = _parse_market_prices(market)
                                        parsed["updated_at"] = time.time()
                                        market_prices[t] = parsed
                            cursor = data.get("cursor", "")
                            if not cursor:
                                break
                            await asyncio.sleep(0.25)  # Rate limit paginated requests too
                    except Exception as e:
                        log.warning(f"Error fetching series {series_ticker}: {e}")

                # Subscribe to any new tickers via WebSocket
                to_add = new_tickers - subscribed_tickers
                if to_add:
                    tickers_list = list(to_add)
                    try:
                        if ticker_sub_sid is None:
                            ticker_sub_sid = await ws.subscribe(["ticker"], tickers_list)
                            lifecycle_sub_sid = await ws.subscribe(
                                ["market_lifecycle_v2"], tickers_list
                            )
                        else:
                            assert ticker_sub_sid is not None
                            assert lifecycle_sub_sid is not None
                            await ws.update_subscription(ticker_sub_sid, tickers_list)
                            await ws.update_subscription(lifecycle_sub_sid, tickers_list)
                        subscribed_tickers.update(to_add)
                        n = len(to_add)
                        total = len(subscribed_tickers)
                        log.info(f"WS: subscribed to {n} new tickers ({total} total)")
                    except Exception as e:
                        log.warning(f"WS subscribe error: {e}")

                # Now evaluate using real-time prices from WS
                await scan_kalshi_with_espn(
                    client,
                    current_espn,
                    cur_price,
                    cur_bet,
                    dry_run,
                    espn_final_period=current_espn_fp,
                )

                # Stop-loss: sell positions that have dropped below threshold
                # Pass both final-minutes and final-period caches for game state lookup
                all_espn = {**current_espn, **current_espn_fp}
                await check_stop_losses(client, all_espn)

                # Settlement checks as fallback (WS lifecycle handles most)
                await check_settlements(client)

                # Sync positions with Kalshi (detect manual closes, fills, settlements)
                await sync_positions(client)

                await record_balance(client)
            except Exception as e:
                import traceback
                err_msg = f"Kalshi scan error: {e}\n{traceback.format_exc()}"
                log.warning(err_msg)
                scan_debug["last_errors"].append(err_msg)
                # Cap error list to prevent unbounded memory growth
                if len(scan_debug["last_errors"]) > 50:
                    scan_debug["last_errors"] = scan_debug["last_errors"][-50:]

            await asyncio.sleep(kalshi_interval)

    async def ws_loop():
        """Maintain WebSocket connection and listen for events."""
        while True:
            try:
                await ws.connect()
                await ws.listen()
            except Exception as e:
                log.warning(f"WS loop error: {e}, restarting in 5s...")
                await asyncio.sleep(5)

    async def backup_loop():
        """Back up DB to S3 every 30 minutes."""
        while True:
            await asyncio.sleep(1800)  # 30 min
            await backup_db()

    # Run all loops concurrently
    await asyncio.gather(espn_loop(), kalshi_scan_loop(), ws_loop(), backup_loop())


if __name__ == "__main__":
    min_price = int(os.getenv("MIN_YES_PRICE", "88"))
    max_bet = int(os.getenv("MAX_BET_AMOUNT_CENTS", "500"))
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
    dry = os.getenv("DRY_RUN", "true").lower() == "true"

    log.info(
        f"Starting scanner: min_price={min_price}c, max_bet={max_bet}c, "
        f"ESPN=10s, Kalshi=5s, dry_run={dry}"
    )
    asyncio.run(
        run_scanner(
            min_yes_price=min_price,
            max_bet_cents=max_bet,
            poll_interval=interval,
            dry_run=dry,
        )
    )
