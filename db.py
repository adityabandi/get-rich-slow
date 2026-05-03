import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Use /data for persistent volume (Railway/Docker), fallback to local dir
_data_dir = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
_default_db = os.path.join(_data_dir, "predictions.db")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{_default_db}",
)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Scan(Base):
    __tablename__ = "scans"

    id = Column(Integer, primary_key=True)
    scanned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    opportunities_found = Column(Integer, default=0)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id = Column(Integer, primary_key=True)
    scan_id = Column(Integer, nullable=True)
    found_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ticker = Column(String, index=True)
    event_ticker = Column(String)
    series_ticker = Column(String)
    title = Column(Text)
    yes_sub_title = Column(Text)
    yes_bid = Column(Integer)
    yes_ask = Column(Integer)
    spread = Column(Integer)
    volume = Column(Integer)
    close_time = Column(String)
    # ESPN game state at time of opportunity
    sport_path = Column(String, nullable=True)
    espn_period = Column(Integer, nullable=True)
    espn_clock = Column(String, nullable=True)
    espn_home = Column(String, nullable=True)
    espn_away = Column(String, nullable=True)
    espn_home_score = Column(Integer, nullable=True)
    espn_away_score = Column(Integer, nullable=True)
    espn_score_diff = Column(Integer, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    placed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ticker = Column(String, index=True)
    event_ticker = Column(String)
    title = Column(Text)
    side = Column(String)  # yes/no
    action = Column(String)  # buy/sell
    count = Column(Integer)
    yes_price = Column(Integer)  # cents
    cost_cents = Column(Integer)
    potential_profit_cents = Column(Integer)
    status = Column(String, default="placed")  # placed, filled, settled_win, settled_loss
    settled_at = Column(DateTime, nullable=True)
    pnl_cents = Column(Integer, nullable=True)
    dry_run = Column(Boolean, default=True)
    order_id = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    # Stop-loss context: game state at entry for monitoring
    sport_path = Column(String, nullable=True)
    entry_lead = Column(Integer, nullable=True)  # score diff when we bought
    series_ticker = Column(String, nullable=True)


class StretchOpportunity(Base):
    """Near-miss markets that didn't quite meet our filters.

    Tracked to see if loosening risk params would be profitable.
    """

    __tablename__ = "stretch_opportunities"

    id = Column(Integer, primary_key=True)
    found_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ticker = Column(String, index=True)
    event_ticker = Column(String)
    series_ticker = Column(String)
    title = Column(Text)
    yes_sub_title = Column(Text)
    yes_ask = Column(Integer)
    volume = Column(Integer)
    sport_path = Column(String)
    score_lead = Column(Integer)
    min_score_lead = Column(Integer)
    espn_period = Column(Integer)
    espn_clock = Column(String)
    # Why it was a stretch (which filter it missed)
    reason = Column(String)  # "price", "score_lead", "time", "confidence"
    # Confidence score at time of discovery
    confidence = Column(Integer, nullable=True)
    # Which what-if strategy set this belongs to
    strategy_set = Column(String, default="default", index=True)
    # Settlement tracking
    status = Column(String, default="open")  # open, settled_win, settled_loss
    pnl_cents = Column(Integer, nullable=True)  # hypothetical P&L


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id = Column(Integer, primary_key=True)
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    balance_cents = Column(Integer)
    portfolio_value_cents = Column(Integer, nullable=True)


class ConfigEntry(Base):
    """Key-value config store for runtime-tunable parameters."""

    __tablename__ = "config"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


# --- Polymarket weather trading models ---


class WeatherMarket(Base):
    """Polymarket weather markets seen by the scanner.

    One row per (token_id) — i.e. per binary YES outcome over a temperature bin.
    """

    __tablename__ = "weather_markets"

    token_id = Column(String, primary_key=True)
    market_slug = Column(String, index=True)
    question = Column(Text)
    city = Column(String, index=True)
    target_date = Column(String, index=True)  # ISO date (YYYY-MM-DD) the market resolves on
    low_f = Column(Float)
    high_f = Column(Float)
    yes_ask = Column(Float)  # implied probability 0..1
    yes_bid = Column(Float, nullable=True)
    volume = Column(Float, default=0.0)
    end_time = Column(DateTime, nullable=True)
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WeatherTrade(Base):
    """Polymarket weather trades — actual or dry-run.

    Mirrors `Trade` but in USDC (floats) and with model/EV context.
    """

    __tablename__ = "weather_trades"

    id = Column(Integer, primary_key=True)
    placed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    token_id = Column(String, index=True)
    market_slug = Column(String, nullable=True)
    question = Column(Text, nullable=True)
    city = Column(String, index=True, nullable=True)
    target_date = Column(String, nullable=True)
    low_f = Column(Float, nullable=True)
    high_f = Column(Float, nullable=True)
    side = Column(String, default="yes")  # yes/no
    action = Column(String, default="buy")  # buy/sell
    size_usdc = Column(Float)  # dollars committed
    yes_price = Column(Float)  # 0..1 probability paid per share
    p_model = Column(Float)  # model probability at entry
    ev = Column(Float)  # expected value per dollar
    kelly_fraction_used = Column(Float)
    status = Column(String, default="placed")  # placed, settled_win, settled_loss, error
    settled_at = Column(DateTime, nullable=True)
    pnl_usdc = Column(Float, nullable=True)
    actual_high_f = Column(Float, nullable=True)
    dry_run = Column(Boolean, default=True)
    order_id = Column(String, nullable=True)
    error = Column(Text, nullable=True)


class WeatherForecastSnapshot(Base):
    """Forecast snapshot by source × city × target date.

    Persisted so we can replay calibration / backtest later.
    """

    __tablename__ = "weather_forecasts"

    id = Column(Integer, primary_key=True)
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    source = Column(String, index=True)
    city = Column(String, index=True)
    target_date = Column(String, index=True)
    mean_f = Column(Float)
    std_f = Column(Float)
    raw_json = Column(Text, nullable=True)


class ForecastCalibration(Base):
    """Per (source, city) forecast accuracy — Brier score numerator/denominator.

    Updated each time a weather trade settles. Used as inverse-weighting input
    to the next ensemble probability calculation. This is the "self-learning" loop.
    """

    __tablename__ = "forecast_calibration"

    source = Column(String, primary_key=True)
    city = Column(String, primary_key=True)
    n = Column(Integer, default=0)
    brier_sum = Column(Float, default=0.0)
    last_updated = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WeatherLesson(Base):
    """Post-settlement lesson written by Claude after each weather trade settles.

    Read-only oversight surface — surfaced via /api/weather-lessons and consumed
    by Base44 (or any other UI) for human review. May include an `approval_status`
    once the approval-queue workflow is built.
    """

    __tablename__ = "weather_lessons"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    trade_id = Column(Integer, index=True, nullable=True)
    city = Column(String, nullable=True)
    target_date = Column(String, nullable=True)
    lesson = Column(Text)  # Claude-written 1–2 sentence summary
    suggested_config_key = Column(String, nullable=True)  # e.g. "weather_min_ev"
    suggested_config_value = Column(String, nullable=True)
    approval_status = Column(String, default="pending")  # pending | approved | rejected
    model = Column(String, nullable=True)  # which Claude model wrote the lesson
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)


def init_db():
    Base.metadata.create_all(engine)
    # Add columns that may not exist in older DBs
    _migrate_add_columns()
    # Migrate stale config defaults to new values
    _migrate_config_defaults()


def _migrate_add_columns():
    """Add columns to existing tables if they don't exist."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "stretch_opportunities" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("stretch_opportunities")}
        if "strategy_set" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE stretch_opportunities "
                        "ADD COLUMN strategy_set VARCHAR DEFAULT 'default'"
                    )
                )
        if "confidence" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE stretch_opportunities "
                        "ADD COLUMN confidence INTEGER"
                    )
                )

    if "trades" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("trades")}
        trade_new_cols = {
            "sport_path": "VARCHAR",
            "entry_lead": "INTEGER",
            "series_ticker": "VARCHAR",
        }
        with engine.begin() as conn:
            for col_name, col_type in trade_new_cols.items():
                if col_name not in cols:
                    conn.execute(
                        text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
                    )

    if "opportunities" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("opportunities")}
        espn_cols = {
            "sport_path": "VARCHAR",
            "espn_period": "INTEGER",
            "espn_clock": "VARCHAR",
            "espn_home": "VARCHAR",
            "espn_away": "VARCHAR",
            "espn_home_score": "INTEGER",
            "espn_away_score": "INTEGER",
            "espn_score_diff": "INTEGER",
        }
        with engine.begin() as conn:
            for col_name, col_type in espn_cols.items():
                if col_name not in cols:
                    conn.execute(
                        text(f"ALTER TABLE opportunities ADD COLUMN {col_name} {col_type}")
                    )


def _migrate_config_defaults():
    """Update stale config DB rows that still hold old default values.

    When we change a _CONFIG_DEFAULTS value, any DB row that was previously
    written with the OLD default will override the new code default. This
    migration updates those rows so the new defaults take effect without
    requiring manual intervention.
    """
    from sqlalchemy import text

    # (key, old_value, new_value) — only updates if current DB value == old_value
    migrations = [
        ("min_yes_price",             "92",   "88"),
        ("max_positions",             "10",   "30"),
        ("max_positions",             "20",   "30"),
        ("tier1_reserved_slots",      "2",    "1"),
        ("tier3_max_slots",           "1",    "3"),
        ("max_daily_loss",            "1000", "2000"),
        ("lead:basketball/nba",       "8",    "12"),
        ("lead:basketball/nba",       "10",   "12"),
        ("lead:basketball/mens-college-basketball", "8",  "12"),
        ("lead:basketball/mens-college-basketball", "10", "12"),
        ("lead:basketball/womens-college-basketball", "10", "12"),
        ("lead:baseball/mlb",         "4",    "3"),
        # Basketball timing: tightened to 3:00 for ~0% loss rate at 30% sizing
        ("final_seconds:basketball/nba",                    "300", "180"),
        ("final_seconds:basketball/nba",                    "240", "180"),
        ("final_seconds:basketball/mens-college-basketball","300", "180"),
        ("final_seconds:basketball/mens-college-basketball","240", "180"),
        ("final_seconds:basketball/womens-college-basketball","300","180"),
        ("final_seconds:basketball/womens-college-basketball","240","180"),
        # Hockey timing: 3:00 with 3-goal lead — near-impossible to blow
        ("final_seconds:hockey/nhl",                        "300", "180"),
        ("final_seconds:hockey/nhl",                        "240", "180"),
        # Football timing: 3:00 with 17pts (3 scores) — not enough time
        ("final_seconds:football/nfl",                      "300", "180"),
        ("final_seconds:football/college-football",         "300", "180"),
        # Score lead tightening (30% sizing demands near-zero loss rate)
        ("lead:basketball/nba",                             "12",  "15"),
        ("lead:basketball/mens-college-basketball",         "12",  "15"),
        ("lead:basketball/womens-college-basketball",       "12",  "15"),
        ("lead:hockey/nhl",                                 "2",   "3"),
        ("lead:football/nfl",                               "14",  "17"),
        ("lead:football/college-football",                  "14",  "17"),
        ("lead:baseball/mlb",                               "3",   "4"),
        # Restore volume-optimized thresholds (reverting efe0125 tightening)
        ("lead:basketball/nba",                             "15",  "12"),
        ("lead:basketball/mens-college-basketball",         "15",  "12"),
        ("lead:basketball/womens-college-basketball",       "15",  "12"),
        ("lead:hockey/nhl",                                 "3",   "2"),
        ("lead:football/nfl",                               "17",  "14"),
        ("lead:football/college-football",                  "17",  "14"),
        ("lead:baseball/mlb",                               "4",   "3"),
        # Restore 5:00 timing windows (reverting efe0125 3:00 tightening)
        ("final_seconds:basketball/nba",                      "180", "300"),
        ("final_seconds:basketball/mens-college-basketball",  "180", "300"),
        ("final_seconds:basketball/womens-college-basketball","180", "300"),
        ("final_seconds:basketball/wnba",                     "180", "300"),
        ("final_seconds:basketball/fiba",                     "180", "300"),
        ("final_seconds:basketball/cba",                      "180", "300"),
        ("final_seconds:basketball/bbl",                      "180", "300"),
        ("final_seconds:basketball/ita.lba",                  "180", "300"),
        ("final_seconds:basketball/esp.acb",                  "180", "300"),
        ("final_seconds:basketball/jpn.bleague",              "180", "300"),
        ("final_seconds:basketball/euroleague",               "180", "300"),
        ("final_seconds:basketball/eurocup",                  "180", "300"),
        ("final_seconds:basketball/gre.gbl",                  "180", "300"),
        ("final_seconds:basketball/tur.bsl",                  "180", "300"),
        ("final_seconds:basketball/arg.lnb",                  "180", "300"),
        ("final_seconds:basketball/fra.lnb",                  "180", "300"),
        ("final_seconds:basketball/aba",                      "180", "300"),
        ("final_seconds:basketball/fiba.cl",                  "180", "300"),
        ("final_seconds:basketball/fiba.ecup",                "180", "300"),
        ("final_seconds:basketball/kor.kbl",                  "180", "300"),
        ("final_seconds:basketball/aus.nbl",                  "180", "300"),
        ("final_seconds:basketball/phl.pba",                  "180", "300"),
        ("final_seconds:hockey/nhl",                          "180", "300"),
        ("final_seconds:hockey/del",                          "180", "300"),
        ("final_seconds:hockey/elh",                          "180", "300"),
        ("final_seconds:hockey/liiga",                        "180", "300"),
        ("final_seconds:hockey/khl",                          "180", "300"),
        ("final_seconds:hockey/shl",                          "180", "300"),
        ("final_seconds:hockey/iihf",                         "180", "300"),
        ("final_seconds:football/nfl",                        "180", "300"),
        ("final_seconds:football/college-football",           "180", "300"),
        # Restore 75th minute for soccer (reverting 80th min tightening)
        ("final_seconds:soccer/eng.1",                 "4800", "4500"),
        ("final_seconds:soccer/esp.1",                 "4800", "4500"),
        ("final_seconds:soccer/ger.1",                 "4800", "4500"),
        ("final_seconds:soccer/fra.1",                 "4800", "4500"),
        ("final_seconds:soccer/ita.1",                 "4800", "4500"),
        ("final_seconds:soccer/usa.1",                 "4800", "4500"),
        ("final_seconds:soccer/ned.1",                 "4800", "4500"),
        ("final_seconds:soccer/por.1",                 "4800", "4500"),
        ("final_seconds:soccer/bel.1",                 "4800", "4500"),
        ("final_seconds:soccer/sco.1",                 "4800", "4500"),
        ("final_seconds:soccer/tur.1",                 "4800", "4500"),
        ("final_seconds:soccer/ger.2",                 "4800", "4500"),
        ("final_seconds:soccer/ita.2",                 "4800", "4500"),
        ("final_seconds:soccer/eng.2",                 "4800", "4500"),
        ("final_seconds:soccer/esp.2",                 "4800", "4500"),
        ("final_seconds:soccer/cze.1",                 "4800", "4500"),
        ("final_seconds:soccer/den.1",                 "4800", "4500"),
        ("final_seconds:soccer/gre.1",                 "4800", "4500"),
        ("final_seconds:soccer/aut.1",                 "4800", "4500"),
        ("final_seconds:soccer/swe.1",                 "4800", "4500"),
        ("final_seconds:soccer/nor.1",                 "4800", "4500"),
        ("final_seconds:soccer/fin.1",                 "4800", "4500"),
        ("final_seconds:soccer/pol.1",                 "4800", "4500"),
        ("final_seconds:soccer/cro.1",                 "4800", "4500"),
        ("final_seconds:soccer/rus.1",                 "4800", "4500"),
        ("final_seconds:soccer/ukr.1",                 "4800", "4500"),
        ("final_seconds:soccer/sui.1",                 "4800", "4500"),
        ("final_seconds:soccer/eng.w.1",               "4800", "4500"),
        ("final_seconds:soccer/eng.fa",                "4800", "4500"),
        ("final_seconds:soccer/eng.league_cup",        "4800", "4500"),
        ("final_seconds:soccer/esp.copa_del_rey",      "4800", "4500"),
        ("final_seconds:soccer/ita.coppa_italia",      "4800", "4500"),
        ("final_seconds:soccer/ger.dfb_pokal",         "4800", "4500"),
        ("final_seconds:soccer/fra.coupe_de_france",   "4800", "4500"),
        ("final_seconds:soccer/esp.super_cup",         "4800", "4500"),
        ("final_seconds:soccer/concacaf.champions",    "4800", "4500"),
        ("final_seconds:soccer/fifa.cwc",              "4800", "4500"),
        ("final_seconds:soccer/conmebol.libertadores", "4800", "4500"),
        ("final_seconds:soccer/conmebol.sudamericana", "4800", "4500"),
        ("final_seconds:soccer/usa.nwsl",              "4800", "4500"),
        ("final_seconds:soccer/usa.usl.c",             "4800", "4500"),
        ("final_seconds:soccer/mex.1",                 "4800", "4500"),
        ("final_seconds:soccer/bra.1",                 "4800", "4500"),
        ("final_seconds:soccer/arg.1",                 "4800", "4500"),
        ("final_seconds:soccer/col.1",                 "4800", "4500"),
        ("final_seconds:soccer/per.1",                 "4800", "4500"),
        ("final_seconds:soccer/ecu.1",                 "4800", "4500"),
        ("final_seconds:soccer/aus.1",                 "4800", "4500"),
        ("final_seconds:soccer/jpn.1",                 "4800", "4500"),
        ("final_seconds:soccer/chn.1",                 "4800", "4500"),
        ("final_seconds:soccer/ksa.1",                 "4800", "4500"),
        ("final_seconds:soccer/isr.1",                 "4800", "4500"),
        ("final_seconds:soccer/ind.1",                 "4800", "4500"),
        ("final_seconds:soccer/uefa.champions",        "4800", "4500"),
        ("final_seconds:soccer/uefa.europa",           "4800", "4500"),
        ("final_seconds:soccer/uefa.europa.conf",      "4800", "4500"),
    ]
    with engine.begin() as conn:
        for key, old_val, new_val in migrations:
            conn.execute(
                text("UPDATE config SET value = :new WHERE key = :key AND value = :old"),
                {"new": new_val, "key": key, "old": old_val},
            )


def get_session():
    return SessionLocal()


# --- Runtime config helpers ---

# Defaults used when no DB override exists
_CONFIG_DEFAULTS: dict[str, str] = {
    "min_yes_price": "88",
    "max_bet_cents": "500",
    "max_positions": "30",
    "min_volume": "100",
    "stretch_price_min": "85",
    # Per-sport score leads
    # Basketball: 12pt lead at 5:00 = 4+ possessions needed (~30s each), ~99%+ safe
    "lead:basketball/nba": "12",
    "lead:basketball/mens-college-basketball": "12",
    "lead:basketball/womens-college-basketball": "12",
    # Hockey: 2 goals — with pulled goalie, 2 goals in 5min is ~1-2% chance
    "lead:hockey/nhl": "2",
    # Football: 14pts = 2 scores in 5:00, ~99%+ safe
    "lead:football/nfl": "14",
    "lead:football/college-football": "14",
    # Baseball: 3 runs — grand slam only ties, can't win
    "lead:baseball/mlb": "3",
    "lead:soccer/eng.1": "2",
    "lead:soccer/esp.1": "2",
    "lead:soccer/usa.1": "2",
    # Per-sport final minutes: 5:00 for basketball/hockey/football, 75th min for soccer
    "final_seconds:basketball/nba": "300",
    "final_seconds:basketball/mens-college-basketball": "300",
    "final_seconds:basketball/womens-college-basketball": "300",
    "final_seconds:basketball/wnba": "300",
    "final_seconds:basketball/fiba": "300",
    # SofaScore basketball — match 5:00 for consistency
    "final_seconds:basketball/cba": "300",
    "final_seconds:basketball/bbl": "300",
    "final_seconds:basketball/ita.lba": "300",
    "final_seconds:basketball/esp.acb": "300",
    "final_seconds:basketball/jpn.bleague": "300",
    "final_seconds:basketball/euroleague": "300",
    "final_seconds:basketball/eurocup": "300",
    "final_seconds:basketball/gre.gbl": "300",
    "final_seconds:basketball/tur.bsl": "300",
    "final_seconds:basketball/arg.lnb": "300",
    "final_seconds:basketball/fra.lnb": "300",
    "final_seconds:basketball/aba": "300",
    "final_seconds:basketball/fiba.cl": "300",
    "final_seconds:basketball/fiba.ecup": "300",
    "final_seconds:basketball/kor.kbl": "300",
    "final_seconds:basketball/aus.nbl": "300",
    "final_seconds:basketball/phl.pba": "300",
    # Hockey — 5:00 remaining (goalie pull window, need 2 goals)
    "final_seconds:hockey/del": "300",
    "final_seconds:hockey/elh": "300",
    "final_seconds:hockey/liiga": "300",
    "final_seconds:hockey/khl": "300",
    "final_seconds:hockey/shl": "300",
    "final_seconds:hockey/iihf": "300",
    "final_seconds:hockey/nhl": "300",
    # Football — 5:00 remaining (14pts = 2 scores)
    "final_seconds:football/nfl": "300",
    "final_seconds:football/college-football": "300",
    # Soccer: all leagues at 75th minute (countup: clock >= 4500s)
    "final_seconds:soccer/eng.1": "4500",
    "final_seconds:soccer/esp.1": "4500",
    "final_seconds:soccer/ger.1": "4500",
    "final_seconds:soccer/fra.1": "4500",
    "final_seconds:soccer/ita.1": "4500",
    "final_seconds:soccer/usa.1": "4500",
    "final_seconds:soccer/ned.1": "4500",
    "final_seconds:soccer/por.1": "4500",
    "final_seconds:soccer/bel.1": "4500",
    "final_seconds:soccer/sco.1": "4500",
    "final_seconds:soccer/tur.1": "4500",
    "final_seconds:soccer/ger.2": "4500",
    "final_seconds:soccer/ita.2": "4500",
    "final_seconds:soccer/eng.2": "4500",
    "final_seconds:soccer/esp.2": "4500",
    "final_seconds:soccer/cze.1": "4500",
    "final_seconds:soccer/den.1": "4500",
    "final_seconds:soccer/gre.1": "4500",
    "final_seconds:soccer/aut.1": "4500",
    "final_seconds:soccer/swe.1": "4500",
    "final_seconds:soccer/nor.1": "4500",
    "final_seconds:soccer/fin.1": "4500",
    "final_seconds:soccer/pol.1": "4500",
    "final_seconds:soccer/cro.1": "4500",
    "final_seconds:soccer/rus.1": "4500",
    "final_seconds:soccer/ukr.1": "4500",
    "final_seconds:soccer/sui.1": "4500",
    "final_seconds:soccer/eng.w.1": "4500",
    "final_seconds:soccer/eng.fa": "4500",
    "final_seconds:soccer/eng.league_cup": "4500",
    "final_seconds:soccer/esp.copa_del_rey": "4500",
    "final_seconds:soccer/ita.coppa_italia": "4500",
    "final_seconds:soccer/ger.dfb_pokal": "4500",
    "final_seconds:soccer/fra.coupe_de_france": "4500",
    "final_seconds:soccer/esp.super_cup": "4500",
    "final_seconds:soccer/concacaf.champions": "4500",
    "final_seconds:soccer/fifa.cwc": "4500",
    "final_seconds:soccer/conmebol.libertadores": "4500",
    "final_seconds:soccer/conmebol.sudamericana": "4500",
    "final_seconds:soccer/usa.nwsl": "4500",
    "final_seconds:soccer/usa.usl.c": "4500",
    "final_seconds:soccer/mex.1": "4500",
    "final_seconds:soccer/bra.1": "4500",
    "final_seconds:soccer/arg.1": "4500",
    "final_seconds:soccer/col.1": "4500",
    "final_seconds:soccer/per.1": "4500",
    "final_seconds:soccer/ecu.1": "4500",
    "final_seconds:soccer/aus.1": "4500",
    "final_seconds:soccer/jpn.1": "4500",
    "final_seconds:soccer/chn.1": "4500",
    "final_seconds:soccer/ksa.1": "4500",
    "final_seconds:soccer/isr.1": "4500",
    "final_seconds:soccer/ind.1": "4500",
    "final_seconds:soccer/uefa.champions": "4500",
    "final_seconds:soccer/uefa.europa": "4500",
    "final_seconds:soccer/uefa.europa.conf": "4500",
    # Sport priority tiers
    "tier1_reserved_slots": "1",
    "tier3_max_slots": "3",
    # Max daily loss (cents) before scanner pauses new bets
    "max_daily_loss": "2000",
    # Portfolio exposure cap: max % of daily starting balance committed at once
    "max_portfolio_exposure_pct": "67",
    # Stop-loss: sell if YES bid drops to this price (cents). 0 = disabled.
    "stop_loss_price": "50",
    # Emergency blind-sell: sell even without game data if bid <= this price.
    # Prevents holding into total loss when ESPN data is unavailable.
    "blind_sell_price": "15",
    # Total deposited into Kalshi account (cents) — used for P&L calculation
    "total_deposited_cents": "29600",
    # WebSocket price-drop sniping: buy immediately when a watchlisted market's
    # ask drops into range, without waiting for the next scan cycle. 1 = enabled.
    "snipe_enabled": "1",
    # Confidence-based bet sizing: scale max_bet_cents by opportunity confidence score.
    # 1 = enabled (85+ = full size, 70-84 = 50%, 55-69 = 35%, <55 = skip)
    "confidence_sizing": "1",
    # --- Polymarket weather trading (parallel module, dry-run only in v1) ---
    # Master switch — false = the weather loop never starts.
    "weather_enabled": "false",
    # Live execution switch — false = dry-run trades only (no Polygon signing).
    "weather_live": "false",
    # Min expected value per $1 of YES bought before we trade. 0.10 = 10% edge.
    "weather_min_ev": "0.10",
    # Min market volume (USDC) before we'll consider trading.
    "weather_min_volume": "2000",
    # Don't buy YES at prices above this (probability) — keeps us in the convex part of Kelly.
    "weather_max_price": "0.45",
    # Max dollars per single weather trade.
    "weather_max_bet_usdc": "2.00",
    # Fractional Kelly multiplier (0.25 = quarter Kelly).
    "weather_kelly_fraction": "0.25",
    # Trade only if hours-to-resolution is in this window.
    "weather_min_hours": "2",
    "weather_max_hours": "72",
    # How often the weather loop runs (seconds).
    "weather_scan_interval_seconds": "600",
    # Comma-separated list of forecast sources to use.
    "weather_sources": "open-meteo,visual-crossing",
    # Comma-separated list of city slugs to scan (matches CITY_COORDS in weather.py).
    "weather_cities": "new-york,chicago,los-angeles,miami,seattle,denver,boston,austin",
    # Per-(city,source) forecast cache TTL.
    "weather_forecast_cache_seconds": "600",
    # Min number of settled trades per (source, city) before its calibration weight applies.
    "weather_calibration_min": "10",
    # Claude post-settlement lesson writer — gated by the ANTHROPIC_API_KEY env var
    # (without a key, lessons are silently skipped). Cheap by design (Haiku 4.5).
    "weather_lessons_enabled": "true",
    "weather_lessons_model": "claude-haiku-4-5",
    "weather_lessons_max_tokens": "200",
}


def get_config(key: str) -> str:
    """Get a config value from DB, falling back to defaults."""
    session = get_session()
    entry = session.query(ConfigEntry).filter_by(key=key).first()
    session.close()
    if entry:
        return entry.value
    return _CONFIG_DEFAULTS.get(key, "")


def get_config_int(key: str) -> int:
    return int(get_config(key) or "0")


def get_config_float(key: str) -> float:
    raw = get_config(key) or "0"
    return float(raw)


def get_config_bool(key: str) -> bool:
    return (get_config(key) or "").strip().lower() in ("1", "true", "yes", "on")


def set_config(key: str, value: str):
    """Set a config value in the DB."""
    session = get_session()
    entry = session.query(ConfigEntry).filter_by(key=key).first()
    if entry:
        entry.value = value
    else:
        session.add(ConfigEntry(key=key, value=value))
    session.commit()
    session.close()


def get_all_config() -> dict[str, str]:
    """Get all config as a dict (defaults merged with DB overrides)."""
    result = dict(_CONFIG_DEFAULTS)
    session = get_session()
    for entry in session.query(ConfigEntry).all():
        result[entry.key] = entry.value
    session.close()
    return result
