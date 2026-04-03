"""
ESPN undocumented API client for live game data.

Provides real-time game clock, score, and period info to determine
if a game is truly in its final minutes (4th quarter, 9th inning, etc).
"""

from dataclasses import dataclass
from typing import Optional

import httpx

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Map Kalshi series prefixes to ESPN sport/league paths
KALSHI_TO_ESPN = {
    # --- Basketball ---
    "KXNBAGAME": "basketball/nba",
    "KXNCAAMBGAME": "basketball/mens-college-basketball",
    "KXNCAAWBGAME": "basketball/womens-college-basketball",
    # --- Football ---
    "KXNFLGAME": "football/nfl",
    "KXNCAAFBGAME": "football/college-football",
    "KXNCAAFCSGAME": "football/college-football",  # FCS
    # --- Hockey ---
    "KXNHLGAME": "hockey/nhl",
    "KXNCAAHOCKEYGAME": "hockey/mens-college-hockey",
    # --- Baseball ---
    "KXMLBGAME": "baseball/mlb",
    "KXMLBSTGAME": "baseball/mlb",
    "KXNCAABBGAME": "baseball/college-baseball",
    # --- MMA / Boxing --- DISABLED: no mathematical lead, one-punch endings
    # "KXUFCFIGHT": "mma/ufc",
    # "KXBOXINGFIGHT": "boxing/pbc",  # ESPN returns 400
    # --- Soccer: Top 5 European leagues ---
    "KXEPLGAME": "soccer/eng.1",
    "KXLALIGAGAME": "soccer/esp.1",
    "KXBUNDESLIGAGAME": "soccer/ger.1",
    "KXLIGUE1GAME": "soccer/fra.1",
    "KXSERIEAGAME": "soccer/ita.1",
    # --- Basketball (additional) ---
    "KXWNBAGAME": "basketball/wnba",
    "KXFIBAGAME": "basketball/fiba",
    # --- Soccer: Other European leagues ---
    "KXEREDIVISIEGAME": "soccer/ned.1",
    "KXLIGAPORTUGALGAME": "soccer/por.1",
    "KXBELGIANPLGAME": "soccer/bel.1",
    "KXCZEFLGAME": "soccer/cze.1",
    "KXEFLCHAMPIONSHIPGAME": "soccer/eng.2",
    "KXEWSLGAME": "soccer/eng.w.1",
    "KXSCOTTISHPREMGAME": "soccer/sco.1",
    "KXSUPERLIGGAME": "soccer/tur.1",
    "KXSWISSSUPERLGAME": "soccer/sui.1",
    "KXBUNDESLIGA2GAME": "soccer/ger.2",
    "KXSERIEBGAME": "soccer/ita.2",
    "KXDANISHSUPERLIGAGAME": "soccer/den.1",
    "KXSUPERLEAGUEGREECEGAME": "soccer/gre.1",
    "KXAUSTBUNDESLIGAGAME": "soccer/aut.1",
    "KXALLSVENSKANGAME": "soccer/swe.1",
    "KXELITESERIEGAME": "soccer/nor.1",
    "KXVEIKKAUSLIIGAGAME": "soccer/fin.1",
    "KXEKSTRAKLASAGAME": "soccer/pol.1",
    "KXHNLGAME": "soccer/cro.1",
    "KXRPLGAME": "soccer/rus.1",
    "KXUKRPLGAME": "soccer/ukr.1",
    # --- Soccer: European cups ---
    "KXUCLGAME": "soccer/uefa.champions",
    "KXUELGAME": "soccer/uefa.europa",
    "KXUECLGAME": "soccer/uefa.europa.conf",
    "KXFACUPGAME": "soccer/eng.fa",
    "KXEFLCUPGAME": "soccer/eng.league_cup",
    "KXCOPADELREYGAME": "soccer/esp.copa_del_rey",
    "KXCOPPAITALIAGAME": "soccer/ita.coppa_italia",
    "KXDFBPOKALGAME": "soccer/ger.dfb_pokal",
    "KXCOUPEDEFRANCEGAME": "soccer/fra.coupe_de_france",
    "KXESPSUPERCUPGAME": "soccer/esp.super_cup",
    "KXCONCACAFCCUPGAME": "soccer/concacaf.champions",
    "KXCLUBWCGAME": "soccer/fifa.cwc",
    "KXCOPALIBERTADORESGAME": "soccer/conmebol.libertadores",
    "KXCOPASUDAMERICANAGAME": "soccer/conmebol.sudamericana",
    # --- Soccer: Americas ---
    "KXMLSGAME": "soccer/usa.1",
    "KXNWSLGAME": "soccer/usa.nwsl",
    "KXUSLCHAMPGAME": "soccer/usa.usl.c",
    "KXLIGAMXGAME": "soccer/mex.1",
    "KXBRASILEIROGAME": "soccer/bra.1",
    "KXARGPREMDIVGAME": "soccer/arg.1",
    "KXDIMAYORGAME": "soccer/col.1",
    "KXPERLIGA1GAME": "soccer/per.1",
    "KXLIGAPROFGAME": "soccer/ecu.1",
    # --- Soccer: Asia / Middle East ---
    "KXJLEAGUEGAME": "soccer/jpn.1",
    "KXCHNSLGAME": "soccer/chn.1",
    "KXSAUDIPLGAME": "soccer/ksa.1",
    "KXISRAELIPLGAME": "soccer/isr.1",
    # --- Soccer: Other European ---
    "KXLALIGA2GAME": "soccer/esp.2",
    # --- Soccer: Other ---
    "KXALEAGUEGAME": "soccer/aus.1",
    "KXINDSLGAME": "soccer/ind.1",
    # --- Australian Football ---
    "KXAFLGAME": "australian-football/afl",
    # --- Lacrosse ---
    "KXNCAALAXGAME": "lacrosse/mens-college-lacrosse",
    "KXNCAAMLAXGAME": "lacrosse/mens-college-lacrosse",
    "KXLAXGAME": "lacrosse/mens-college-lacrosse",
    "KXPLLGAME": "lacrosse/pll",
    # --- Cricket --- DISABLED: T20 chase format unpredictable, ESPN data unreliable
    # "KXIPLGAME": "cricket/ipl",
}

# How many periods/quarters each sport has in regulation
# Only non-obvious final periods — soccer/baseball use sport-level defaults
SPORT_FINAL_PERIOD = {
    "basketball/nba": 4,
    "basketball/mens-college-basketball": 2,
    "basketball/womens-college-basketball": 4,
    "basketball/wnba": 4,
    "basketball/fiba": 4,
    "football/nfl": 4,
    "football/college-football": 4,
    "hockey/nhl": 3,
    "hockey/mens-college-hockey": 3,
    "mma/ufc": 5,
    "lacrosse/mens-college-lacrosse": 4,
    "lacrosse/pll": 4,
    "australian-football/afl": 4,
    "cricket/ipl": 2,
}


def get_final_period(sport_path: str) -> int:
    """Return the final period number for a sport path.

    Uses explicit overrides first, then sport-level defaults:
    soccer=2 (halves), baseball=9 (innings), everything else=4 (quarters).
    """
    if sport_path in SPORT_FINAL_PERIOD:
        return SPORT_FINAL_PERIOD[sport_path]
    if sport_path.startswith("soccer/"):
        return 2
    if sport_path.startswith("baseball/"):
        return 9
    return 4


def merge_final_periods(extra: dict) -> None:
    """Merge additional sport final periods (e.g. from SofaScore)."""
    SPORT_FINAL_PERIOD.update(extra)


@dataclass
class GameState:
    """Live state of a game from ESPN."""

    espn_id: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    period: int
    display_clock: str
    clock_seconds: float
    state: str  # "pre", "in", "post"
    status_name: str  # e.g. "STATUS_IN_PROGRESS", "STATUS_FINAL"
    sport_path: str
    home_full_name: str = ""  # Full team name for title matching (e.g. "Zhejiang Lions")
    away_full_name: str = ""  # Full team name for title matching (e.g. "Jiangsu Dragons")
    status_detail: str = ""  # e.g. "Top 9th", "Bot 9th", "Mid 9th"

    @property
    def final_period(self) -> int:
        return get_final_period(self.sport_path)

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    @property
    def is_final_period(self) -> bool:
        return self.period >= self.final_period

    @property
    def is_overtime(self) -> bool:
        return self.period > self.final_period

    @property
    def is_in_final_minutes(self) -> bool:
        """True if game is live, in the final period, with <=5 min on the clock."""
        if not self.is_live:
            return False
        if not self.is_final_period:
            return False
        # Baseball: always require bottom/mid half-inning (including extra innings)
        # Top of any inning = trailing team still has full at-bat, too risky
        if "baseball" in self.sport_path:
            detail_lower = self.status_detail.lower()
            if "bot" in detail_lower or "mid" in detail_lower:
                return True
            # If home team leads in top half, game ends without bottom half
            if "top" in detail_lower and self.home_score > self.away_score:
                return True
            return False

        # Hockey shootout (period > OT) — coin flip, never bet
        if "hockey" in self.sport_path and self.period > self.final_period + 1:
            return False

        # Overtime: tighten to <=2:00 for countdown sports (basketball, hockey, football)
        # OT is desperation play — a 13pt NBA lead evaporated in 3 min
        if self.is_overtime and "soccer" not in self.sport_path:
            return self.clock_seconds <= 120

        # Soccer extra time (cup games): require >= 115th minute (last 5 min of ET)
        if "soccer" in self.sport_path and self.is_overtime:
            return self.clock_seconds >= 6900  # 115 * 60

        from db import get_config_int

        final_secs = get_config_int(f"final_seconds:{self.sport_path}")
        # Soccer clock counts UP — require >= threshold
        if "soccer" in self.sport_path:
            return self.clock_seconds >= (final_secs or 4500)
        return self.clock_seconds <= (final_secs or 300)

    @property
    def score_diff(self) -> int:
        return abs(self.home_score - self.away_score)

    @property
    def leading_team(self) -> str:
        if self.home_score > self.away_score:
            return self.home_team
        elif self.away_score > self.home_score:
            return self.away_team
        return "tied"


async def get_scoreboard(sport_path: str) -> list[GameState]:
    """Fetch live scoreboard for a sport from ESPN."""
    url = f"{ESPN_BASE}/{sport_path}/scoreboard"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    games = []
    for event in data.get("events", []):
        competitions = event.get("competitions", [])
        if not competitions:
            continue
        comp = competitions[0]
        status = comp.get("status", {})
        status_type = status.get("type", {})

        competitors = comp.get("competitors", [])
        home = away = None
        for c in competitors:
            if c.get("homeAway") == "home":
                home = c
            else:
                away = c

        if not home or not away:
            continue

        clock_str = status.get("displayClock", "0:00")
        # Parse clock to seconds
        # ESPN formats:
        #   "5:00" (mm:ss countdown)
        #   "80'" (soccer minutes)
        #   "45'+4'" (soccer stoppage time = 45+4 = 49th minute)
        #   "45" (bare number)
        clock_seconds = 0.0
        try:
            clean = clock_str.strip()
            # Handle soccer stoppage time: "45'+4'" → 45+4 = 49 minutes
            if "+" in clean and "soccer" in sport_path:
                parts = clean.replace("'", "").split("+")
                total_mins = sum(float(p.strip()) for p in parts if p.strip())
                clock_seconds = total_mins * 60
            else:
                # Strip trailing apostrophes
                clean = clean.rstrip("'").strip()
                parts = clean.split(":")
                if len(parts) == 2:
                    clock_seconds = int(parts[0]) * 60 + float(parts[1])
                elif len(parts) == 1:
                    num = float(clean)
                    if "soccer" in sport_path:
                        clock_seconds = num * 60
                    else:
                        clock_seconds = num
        except (ValueError, IndexError):
            # -1 signals unknown clock: countdown check (<=300) will be False,
            # count-up check (>=4500) will also be False — safe default
            clock_seconds = -1.0

        games.append(
            GameState(
                espn_id=event.get("id", ""),
                home_team=home.get("team", {}).get("abbreviation", ""),
                away_team=away.get("team", {}).get("abbreviation", ""),
                home_score=int(home.get("score", "0")),
                away_score=int(away.get("score", "0")),
                period=status.get("period", 0),
                display_clock=clock_str,
                clock_seconds=clock_seconds,
                state=status_type.get("state", ""),
                status_name=status_type.get("name", ""),
                sport_path=sport_path,
                status_detail=status_type.get("shortDetail", "") or status_type.get("detail", ""),
            )
        )

    return games


async def get_live_final_minutes_games() -> dict[str, list[GameState]]:
    """Get all games across all sports that are in their final minutes."""
    result = {}
    for kalshi_series, sport_path in KALSHI_TO_ESPN.items():
        games = await get_scoreboard(sport_path)
        final_min = [g for g in games if g.is_in_final_minutes]
        if final_min:
            result[kalshi_series] = final_min
    return result


async def get_categorized_games() -> tuple[dict[str, list[GameState]], dict[str, list[GameState]]]:
    """Fetch live games once, return (final_minutes, final_period) dicts.

    final_minutes: games matching the configured end-of-game timing.
    final_period: all live games in their final period (broader net for what-ifs).
    Deduplicates ESPN API calls by sport_path to avoid redundant fetches.
    """
    final_minutes: dict[str, list[GameState]] = {}
    final_period: dict[str, list[GameState]] = {}

    # Deduplicate: fetch each sport_path only once
    sport_cache: dict[str, list[GameState]] = {}
    for kalshi_series, sport_path in KALSHI_TO_ESPN.items():
        if sport_path not in sport_cache:
            sport_cache[sport_path] = await get_scoreboard(sport_path)
        games = sport_cache[sport_path]
        fm = [g for g in games if g.is_in_final_minutes]
        fp = [g for g in games if g.is_live and g.is_final_period]
        if fm:
            final_minutes[kalshi_series] = fm
        if fp:
            final_period[kalshi_series] = fp
    return final_minutes, final_period


def game_meets_timing(game: GameState, countdown_secs: int, countup_secs: int) -> bool:
    """Check if a game meets a specific timing threshold."""
    if not game.is_live or not game.is_final_period:
        return False
    # Baseball: always require bottom/mid half-inning (including extra innings)
    if "baseball" in game.sport_path:
        detail_lower = game.status_detail.lower()
        if "bot" in detail_lower or "mid" in detail_lower:
            return True
        if "top" in detail_lower and game.home_score > game.away_score:
            return True
        return False
    # Hockey shootout — coin flip, never bet
    if "hockey" in game.sport_path and game.period > game.final_period + 1:
        return False
    # Overtime: tighten to <=2:00 for countdown sports
    if game.is_overtime and "soccer" not in game.sport_path:
        return game.clock_seconds <= 120
    # Soccer extra time: require >= 115th minute
    if "soccer" in game.sport_path and game.is_overtime:
        return game.clock_seconds >= 6900
    if "soccer" in game.sport_path:
        return game.clock_seconds >= countup_secs
    return game.clock_seconds <= countdown_secs


# ESPN abbreviations that differ from Kalshi ticker abbreviations.
# Only entries where ESPN and Kalshi use DIFFERENT codes.
# Map ESPN abbreviation → list of Kalshi equivalents to check.
ESPN_TO_KALSHI_ABBR: dict[str, list[str]] = {
    # NBA
    "GS": ["GSW"],
    "UTAH": ["UTA"],
    "SA": ["SAS"],
    # MLS
    "DC": ["DCU"],
    "LA": ["LAG", "LA"],  # ESPN "LA" = LA Galaxy; Kalshi uses "LAG"
    # NFL
    "JAX": ["JAC"],
}


def _espn_to_kalshi_codes(espn_abbr: str) -> list[str]:
    """Return Kalshi codes that an ESPN abbreviation could map to."""
    codes = ESPN_TO_KALSHI_ABBR.get(espn_abbr.upper(), [])
    # Always include the original ESPN abbreviation itself
    if espn_abbr.upper() not in [c.upper() for c in codes]:
        codes = [espn_abbr.upper()] + codes
    return codes


def match_kalshi_to_espn(
    kalshi_ticker: str,
    kalshi_title: str,
    espn_games: list[GameState],
) -> Optional[GameState]:
    """
    Try to match a Kalshi market to an ESPN game by team abbreviations.
    Kalshi tickers contain team abbreviations (e.g., KXNBAGAME-26MAR07NYKLAC-LAC).

    Uses exact abbreviation matching first, then falls back to fuzzy title matching
    for soccer leagues where team names may differ between ESPN and Kalshi.
    """
    ticker_upper = kalshi_ticker.upper()
    title_upper = kalshi_title.upper()

    for game in espn_games:
        home_codes = _espn_to_kalshi_codes(game.home_team)
        away_codes = _espn_to_kalshi_codes(game.away_team)

        home_match = any(c in ticker_upper or c in title_upper for c in home_codes)
        away_match = any(c in ticker_upper or c in title_upper for c in away_codes)

        # Require BOTH teams to match — single-team matches cause false positives
        # against future games (e.g., tomorrow's MIL game matching today's MIL game)
        if home_match and away_match:
            return game

    # Fallback 2: fuzzy title matching using full team names
    # Kalshi titles contain full team names like "Zhejiang Lions at Jiangsu Dragons"
    # Works for all sports, especially international leagues from SofaScore
    for game in espn_games:
        # Try full names first (from SofaScore)
        names_to_check = []
        if game.home_full_name:
            names_to_check.append(game.home_full_name.upper())
        if game.away_full_name:
            names_to_check.append(game.away_full_name.upper())

        if len(names_to_check) == 2:
            # Both full names available — check if both appear in title
            home_in = names_to_check[0] in title_upper
            away_in = names_to_check[1] in title_upper
            if home_in and away_in:
                return game
            # Also try partial name matching (first word of each team name)
            home_first = names_to_check[0].split()[0] if names_to_check[0] else ""
            away_first = names_to_check[1].split()[0] if names_to_check[1] else ""
            if home_first and away_first and len(home_first) >= 5 and len(away_first) >= 5:
                if home_first in title_upper and away_first in title_upper:
                    return game

        # Try abbreviation matching in ticker team codes
        # Kalshi ticker format: KXCBAGAME-26MAR22ZHEJIA-ZHE
        import re
        parts = ticker_upper.split("-")
        if len(parts) >= 2:
            teams_part = parts[1]
            teams_only = re.sub(r"^\d+[A-Z]{3}\d+", "", teams_part)
            if len(teams_only) >= 6:
                home_codes = _espn_to_kalshi_codes(game.home_team)
                away_codes = _espn_to_kalshi_codes(game.away_team)
                home_in_teams = any(c in teams_only for c in home_codes)
                away_in_teams = any(c in teams_only for c in away_codes)
                if home_in_teams and away_in_teams:
                    return game

    return None
