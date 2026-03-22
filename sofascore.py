"""
SofaScore live game data for international leagues not covered by ESPN.

Provides real-time scores, period, and clock info for leagues like
KBL (Korea), CBA (China), BBL (Germany), ACB (Spain), B.League (Japan),
Euroleague, and more.

SofaScore is the same source Kalshi uses for settlement verification.
"""

import asyncio
import json
import logging
import urllib.request
from typing import Optional

from espn import GameState

log = logging.getLogger(__name__)

# Try multiple base URLs — api.sofascore.com blocks datacenter IPs,
# but www.sofascore.com/api often works from cloud servers
SOFASCORE_BASES = [
    "https://api.sofascore.com/api/v1",
    "https://www.sofascore.com/api/v1",
]
SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}
# Track which base URL works (avoids retrying the blocked one every time)
_working_base: str | None = None

# Map Kalshi series → SofaScore sport + tournament keywords for matching.
# We fetch ALL live events for a sport, then filter by tournament name.
# "sport_path" is a synthetic path used for score lead / timing lookups.
KALSHI_TO_SOFASCORE: dict[str, dict] = {
    # --- Basketball (international) ---
    "KXCBAGAME": {
        "sport": "basketball",
        "keywords": ["Chinese Basketball Association", "CBA"],
        "sport_path": "basketball/cba",
    },
    "KXBBLGAME": {
        "sport": "basketball",
        "keywords": ["Basketball Bundesliga", "BBL"],
        "sport_path": "basketball/bbl",
    },
    "KXBBSERIEAGAME": {
        "sport": "basketball",
        "keywords": ["Lega Basket", "Serie A"],  # Italian basketball
        "sport_path": "basketball/ita.lba",
    },
    "KXACBGAME": {
        "sport": "basketball",
        "keywords": ["Liga ACB", "Liga Endesa"],
        "sport_path": "basketball/esp.acb",
    },
    "KXGBJLEAGUEGAME": {
        "sport": "basketball",
        "keywords": ["B.League", "B1 League", "B2 League"],
        "sport_path": "basketball/jpn.bleague",
    },
    "KXEUROLEAGUEGAME": {
        "sport": "basketball",
        "keywords": ["Euroleague"],
        "sport_path": "basketball/euroleague",
    },
    "KXEUROCUPGAME": {
        "sport": "basketball",
        "keywords": ["Eurocup"],
        "sport_path": "basketball/eurocup",
    },
    "KXGBLGAME": {
        "sport": "basketball",
        "keywords": ["Greek Basket League", "GBL", "A1 League"],
        "sport_path": "basketball/gre.gbl",
    },
    "KXBSLGAME": {
        "sport": "basketball",
        "keywords": ["BSL", "Turkish Basketball"],
        "sport_path": "basketball/tur.bsl",
    },
    "KXARGLNBGAME": {
        "sport": "basketball",
        "keywords": ["Liga Nacional de Basquetbol", "Argentina"],
        "sport_path": "basketball/arg.lnb",
    },
    "KXLNBELITEGAME": {
        "sport": "basketball",
        "keywords": ["LNB Elite", "Betclic Elite"],  # France
        "sport_path": "basketball/fra.lnb",
    },
    "KXABAGAME": {
        "sport": "basketball",
        "keywords": ["ABA League"],
        "sport_path": "basketball/aba",
    },
    "KXFIBACHAMPLEAGUEGAME": {
        "sport": "basketball",
        "keywords": ["FIBA Champions League", "Basketball Champions League"],
        "sport_path": "basketball/fiba.cl",
    },
    "KXFIBAECUPGAME": {
        "sport": "basketball",
        "keywords": ["FIBA Europe Cup"],
        "sport_path": "basketball/fiba.ecup",
    },
    # --- Ice Hockey (international) ---
    "KXAHLGAME": {
        "sport": "ice-hockey",
        "keywords": ["AHL"],
        "sport_path": "hockey/ahl",
    },
    "KXDELGAME": {
        "sport": "ice-hockey",
        "keywords": ["DEL"],  # German hockey
        "sport_path": "hockey/del",
    },
    "KXELHGAME": {
        "sport": "ice-hockey",
        "keywords": ["ELH", "Extraliga"],  # Czech hockey
        "sport_path": "hockey/elh",
    },
    "KXLIIGAGAME": {
        "sport": "ice-hockey",
        "keywords": ["Liiga"],  # Finnish hockey
        "sport_path": "hockey/liiga",
    },
}

# Final period count for SofaScore-only leagues
SOFASCORE_FINAL_PERIOD = {
    # Basketball — all use 4 quarters
    "basketball/cba": 4,
    "basketball/bbl": 4,
    "basketball/ita.lba": 4,
    "basketball/esp.acb": 4,
    "basketball/jpn.bleague": 4,
    "basketball/euroleague": 4,
    "basketball/eurocup": 4,
    "basketball/gre.gbl": 4,
    "basketball/tur.bsl": 4,
    "basketball/arg.lnb": 4,
    "basketball/fra.lnb": 4,
    "basketball/aba": 4,
    "basketball/fiba.cl": 4,
    "basketball/fiba.ecup": 4,
    # Ice Hockey — 3 periods
    "hockey/ahl": 3,
    "hockey/del": 3,
    "hockey/elh": 3,
    "hockey/liiga": 3,
}

# Score leads for SofaScore leagues
SOFASCORE_SCORE_LEAD = {
    # Basketball — 8 point lead
    "basketball/cba": 8,
    "basketball/bbl": 8,
    "basketball/ita.lba": 8,
    "basketball/esp.acb": 8,
    "basketball/jpn.bleague": 8,
    "basketball/euroleague": 8,
    "basketball/eurocup": 8,
    "basketball/gre.gbl": 8,
    "basketball/tur.bsl": 8,
    "basketball/arg.lnb": 8,
    "basketball/fra.lnb": 8,
    "basketball/aba": 8,
    "basketball/fiba.cl": 8,
    "basketball/fiba.ecup": 8,
    # Ice Hockey — 2 goal lead
    "hockey/ahl": 2,
    "hockey/del": 2,
    "hockey/elh": 2,
    "hockey/liiga": 2,
}

# Cache of live events per sport to avoid redundant API calls
_live_cache: dict[str, tuple[float, list]] = {}
CACHE_TTL = 8  # seconds


def _parse_period(event: dict) -> int:
    """Extract current period number from SofaScore event."""
    # Try lastPeriod field: "period1", "period2", etc.
    last_period = event.get("lastPeriod", "")
    if last_period.startswith("period"):
        try:
            return int(last_period.replace("period", ""))
        except ValueError:
            pass
    if last_period == "overtime":
        # Overtime counts as beyond final period
        total = event.get("time", {}).get("totalPeriodCount", 4)
        return total + 1

    # Fallback: calculate from time played
    time_info = event.get("time", {})
    played = time_info.get("played", 0)
    period_len = time_info.get("periodLength", 600)
    if period_len > 0:
        return min(int(played / period_len) + 1, time_info.get("totalPeriodCount", 4))

    return 1


def _parse_clock_remaining(event: dict) -> float:
    """Calculate clock remaining in current period (seconds)."""
    time_info = event.get("time", {})
    played = time_info.get("played", 0)
    period_len = time_info.get("periodLength", 600)

    if period_len <= 0:
        return 0.0

    # Time elapsed in current period
    elapsed_in_period = played % period_len
    # At exact period boundary (elapsed_in_period == 0 and played > 0),
    # we're at the end of the previous period, not the start of the next
    if elapsed_in_period == 0 and played > 0:
        return 0.0
    remaining = period_len - elapsed_in_period
    return float(remaining)


def _event_to_game_state(event: dict, sport_path: str) -> Optional[GameState]:
    """Convert a SofaScore event to our GameState format."""
    status = event.get("status", {})
    status_type = status.get("type", "")

    if status_type != "inprogress":
        return None

    home_team = event.get("homeTeam", {})
    away_team = event.get("awayTeam", {})
    home_score = event.get("homeScore", {}).get("current", 0)
    away_score = event.get("awayScore", {}).get("current", 0)

    # Use nameCode (3-letter abbreviation) like ESPN does
    home_abbr = home_team.get("nameCode", home_team.get("shortName", "???"))
    away_abbr = away_team.get("nameCode", away_team.get("shortName", "???"))

    period = _parse_period(event)
    clock_remaining = _parse_clock_remaining(event)

    # Format display clock as MM:SS
    mins = int(clock_remaining // 60)
    secs = int(clock_remaining % 60)
    display_clock = f"{mins}:{secs:02d}"

    return GameState(
        espn_id=f"ss-{event.get('id', '')}",  # prefix to avoid collision with ESPN IDs
        home_team=home_abbr,
        away_team=away_abbr,
        home_score=home_score,
        away_score=away_score,
        period=period,
        display_clock=display_clock,
        clock_seconds=clock_remaining,  # remaining seconds in current period
        state="in",
        status_name=status.get("description", ""),
        sport_path=sport_path,
    )


def _fetch_sofascore_sync(sport: str) -> list[dict]:
    """Synchronous fetch from SofaScore using urllib (httpx gets 403'd)."""
    global _working_base

    bases_to_try = []
    if _working_base:
        bases_to_try.append(_working_base)
    for b in SOFASCORE_BASES:
        if b not in bases_to_try:
            bases_to_try.append(b)

    for base_url in bases_to_try:
        url = f"{base_url}/sport/{sport}/events/live"
        try:
            req = urllib.request.Request(url, headers=SOFASCORE_HEADERS)
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            events = data.get("events", [])
            _working_base = base_url
            return events
        except Exception:
            continue

    return []


async def get_live_events(sport: str) -> list[dict]:
    """Fetch all live events for a sport from SofaScore, with caching."""
    import time as time_mod

    now = time_mod.time()
    cached = _live_cache.get(sport)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]

    # Run urllib in a thread pool to avoid blocking the async loop
    loop = asyncio.get_event_loop()
    try:
        events = await loop.run_in_executor(None, _fetch_sofascore_sync, sport)
        _live_cache[sport] = (now, events)
        return events
    except Exception as e:
        log.warning(f"SofaScore fetch failed for {sport}: {e}")
        return cached[1] if cached else []


def _matches_tournament(event: dict, keywords: list[str]) -> bool:
    """Check if a SofaScore event's tournament matches any of the keywords."""
    tournament = event.get("tournament", {})
    unique_tournament = tournament.get("uniqueTournament", {})

    # Check tournament name, unique tournament name, and category
    names_to_check = [
        tournament.get("name", ""),
        unique_tournament.get("name", ""),
        tournament.get("slug", ""),
        unique_tournament.get("slug", ""),
    ]

    combined = " ".join(names_to_check).lower()
    return any(kw.lower() in combined for kw in keywords)


async def get_sofascore_games(series: str) -> list[GameState]:
    """Get live games from SofaScore for a specific Kalshi series."""
    config = KALSHI_TO_SOFASCORE.get(series)
    if not config:
        return []

    sport = config["sport"]
    keywords = config["keywords"]
    sport_path = config["sport_path"]

    events = await get_live_events(sport)
    games = []

    for event in events:
        if not _matches_tournament(event, keywords):
            continue
        game = _event_to_game_state(event, sport_path)
        if game:
            games.append(game)

    return games


async def get_all_sofascore_games() -> dict[str, list[GameState]]:
    """Get all live SofaScore games, keyed by Kalshi series."""
    # Collect unique sports to fetch
    sports_needed: set[str] = set()
    for config in KALSHI_TO_SOFASCORE.values():
        sports_needed.add(config["sport"])

    # Fetch all sports in parallel (but SofaScore caches, so fast)
    for sport in sports_needed:
        await get_live_events(sport)

    # Now match events to series
    result: dict[str, list[GameState]] = {}
    for series, config in KALSHI_TO_SOFASCORE.items():
        games = await get_sofascore_games(series)
        if games:
            result[series] = games

    return result


def get_sofascore_series() -> list[str]:
    """Return list of all Kalshi series covered by SofaScore."""
    return list(KALSHI_TO_SOFASCORE.keys())
