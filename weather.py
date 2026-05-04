"""Weather forecast aggregation for Polymarket weather trading.

Plays the role for `weather_scanner.py` that `espn.py` plays for the Kalshi
sports scanner: pulls live external data, normalizes it into a typed shape,
and answers the one question the scanner actually needs — "what is the
probability the high in city X on date D lands in the [low, high)°F bin?"

Free / no-key sources tried first; Visual Crossing only if `VC_API_KEY` is set.

Probability model:
    Treat each source's forecast as a Normal(μ, σ²) over the daily high.
    Combine sources with weights from `forecast_calibration` (inverse Brier).
    Return P(low - 0.5 ≤ X < high + 0.5) — the ±0.5 corresponds to the integer
    cutoff Polymarket uses to bin observations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Hard-coded city → (lat, lon, ICAO METAR station, display name) — KISS.
# Slugs match polymarket question regexes (lowercase, hyphenated).
CITY_COORDS: dict[str, dict[str, Any]] = {
    "new-york":    {"lat": 40.7128, "lon": -74.0060, "icao": "KNYC", "name": "New York"},
    "chicago":     {"lat": 41.8781, "lon": -87.6298, "icao": "KORD", "name": "Chicago"},
    "los-angeles": {"lat": 34.0522, "lon": -118.2437, "icao": "KLAX", "name": "Los Angeles"},
    "miami":       {"lat": 25.7617, "lon": -80.1918, "icao": "KMIA", "name": "Miami"},
    "seattle":     {"lat": 47.6062, "lon": -122.3321, "icao": "KSEA", "name": "Seattle"},
    "denver":      {"lat": 39.7392, "lon": -104.9903, "icao": "KDEN", "name": "Denver"},
    "boston":      {"lat": 42.3601, "lon": -71.0589, "icao": "KBOS", "name": "Boston"},
    "austin":      {"lat": 30.2672, "lon": -97.7431, "icao": "KAUS", "name": "Austin"},
    "phoenix":     {"lat": 33.4484, "lon": -112.0740, "icao": "KPHX", "name": "Phoenix"},
    "philadelphia": {"lat": 39.9526, "lon": -75.1652, "icao": "KPHL", "name": "Philadelphia"},
}


def slug_for_city_name(name: str) -> str | None:
    """Normalize a free-text city name to a known slug, or None if unknown."""
    norm = name.strip().lower().replace(",", "").replace(".", "")
    norm = norm.replace(" ", "-")
    if norm in CITY_COORDS:
        return norm
    # Tolerate "new york city" → "new-york"
    if norm.startswith("new-york"):
        return "new-york"
    if norm.startswith("la-") or norm == "la":
        return "los-angeles"
    return None


@dataclass
class WeatherForecast:
    source: str
    city: str
    target_date: str  # ISO date
    mean_f: float
    std_f: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


# --- Source fetchers ---


async def fetch_open_meteo(
    client: httpx.AsyncClient, city: str, target_date: str
) -> WeatherForecast | None:
    """Open-Meteo ensemble high temp for a date. Free, no key.

    Uses the GFS+ECMWF ensemble forecast endpoint; reports the ensemble of
    daily-max temperatures (we average across hourly members for the date).
    """
    coords = CITY_COORDS.get(city)
    if not coords:
        return None
    params = {
        "latitude": coords["lat"],
        "longitude": coords["lon"],
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "models": "gfs_seamless,ecmwf_ifs025",
        "start_date": target_date,
        "end_date": target_date,
        "timezone": "auto",
    }
    try:
        resp = await client.get(
            "https://ensemble-api.open-meteo.com/v1/ensemble",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning(f"open-meteo fetch failed for {city} {target_date}: {e}")
        return None

    hourly = data.get("hourly") or {}
    members: list[list[float]] = []
    for k, v in hourly.items():
        if k.startswith("temperature_2m") and isinstance(v, list):
            cleaned = [x for x in v if isinstance(x, (int, float))]
            if cleaned:
                members.append(cleaned)
    if not members:
        return None

    # Per-member daily max, then mean/std across members.
    member_highs = [max(m) for m in members]
    if len(member_highs) < 2:
        # Single deterministic run — fall back to point estimate.
        mean = member_highs[0]
        std = 1.5
    else:
        mean = statistics.mean(member_highs)
        std = max(statistics.pstdev(member_highs), 0.5)

    return WeatherForecast(
        source="open-meteo",
        city=city,
        target_date=target_date,
        mean_f=float(mean),
        std_f=float(std),
        raw={"member_highs": member_highs},
    )


async def fetch_visual_crossing(
    client: httpx.AsyncClient, city: str, target_date: str
) -> WeatherForecast | None:
    """Visual Crossing daily forecast. Requires VC_API_KEY env var."""
    api_key = os.getenv("VC_API_KEY") or os.getenv("VISUAL_CROSSING_API_KEY")
    if not api_key:
        return None
    coords = CITY_COORDS.get(city)
    if not coords:
        return None
    location = f"{coords['lat']},{coords['lon']}"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
        f"timeline/{location}/{target_date}/{target_date}"
    )
    params = {
        "key": api_key,
        "unitGroup": "us",
        "include": "days",
        "elements": "tempmax,tempmin",
    }
    try:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning(f"visual-crossing fetch failed for {city} {target_date}: {e}")
        return None

    days = data.get("days") or []
    if not days:
        return None
    tmax = days[0].get("tempmax")
    if tmax is None:
        return None
    # VC is a deterministic forecast — assume σ ≈ 1.8°F as a working baseline.
    return WeatherForecast(
        source="visual-crossing",
        city=city,
        target_date=target_date,
        mean_f=float(tmax),
        std_f=1.8,
        raw={"tempmax": tmax, "tempmin": days[0].get("tempmin")},
    )


async def fetch_visual_crossing_actual(
    client: httpx.AsyncClient, city: str, target_date: str
) -> float | None:
    """Visual Crossing OBSERVED daily high for resolved markets. Same endpoint."""
    api_key = os.getenv("VC_API_KEY") or os.getenv("VISUAL_CROSSING_API_KEY")
    if not api_key:
        return None
    coords = CITY_COORDS.get(city)
    if not coords:
        return None
    location = f"{coords['lat']},{coords['lon']}"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
        f"timeline/{location}/{target_date}/{target_date}"
    )
    params = {"key": api_key, "unitGroup": "us", "include": "days", "elements": "tempmax"}
    try:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        days = resp.json().get("days") or []
        if not days:
            return None
        tmax = days[0].get("tempmax")
        return float(tmax) if tmax is not None else None
    except (httpx.HTTPError, ValueError) as e:
        log.warning(f"VC actual fetch failed for {city} {target_date}: {e}")
        return None


# --- In-memory TTL cache ---

_cache: dict[tuple[str, str, str], tuple[float, WeatherForecast | None]] = {}


async def fetch_forecast(
    client: httpx.AsyncClient, source: str, city: str, target_date: str, cache_ttl: int
) -> WeatherForecast | None:
    key = (source, city, target_date)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and (now - cached[0]) < cache_ttl:
        return cached[1]

    if source == "open-meteo":
        f = await fetch_open_meteo(client, city, target_date)
    elif source == "visual-crossing":
        f = await fetch_visual_crossing(client, city, target_date)
    else:
        log.warning(f"Unknown weather source: {source}")
        return None

    _cache[key] = (now, f)
    return f


# --- Probability math ---


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def probability_in_bin(
    forecasts: list[WeatherForecast],
    low_f: float,
    high_f: float,
    weights: dict[str, float] | None = None,
) -> float:
    """Combine source forecasts and return P(low ≤ daily-high < high+1).

    The Polymarket convention for "between 82 and 83°F" is "high rounds to
    82 or 83" — i.e. the bin covers [low - 0.5, high + 0.5). We accept any
    forecast whose `mean_f` is finite and `std_f` > 0.
    """
    valid = [f for f in forecasts if f.std_f and f.std_f > 0 and math.isfinite(f.mean_f)]
    if not valid:
        return 0.0

    if weights is None:
        weights = {f.source: 1.0 for f in valid}
    raw_w = [max(weights.get(f.source, 1.0), 1e-6) for f in valid]
    total_w = sum(raw_w)
    norm_w = [w / total_w for w in raw_w]

    # Weighted mean and weighted variance (treating each source as Normal).
    mean = sum(w * f.mean_f for w, f in zip(norm_w, valid))
    var = sum(w * (f.std_f ** 2 + (f.mean_f - mean) ** 2) for w, f in zip(norm_w, valid))
    sigma = math.sqrt(max(var, 1e-6))

    lo = (low_f - 0.5 - mean) / sigma
    hi = (high_f + 0.5 - mean) / sigma
    return max(0.0, _phi(hi) - _phi(lo))


def probability_locked_in_bin(
    current_max_f: float,
    bin_low_f: float,
    bin_high_f: float,
    hours_remaining: float,
    hourly_rise_std_f: float = 1.5,
) -> float:
    """Probability the *daily* high lands in [low-0.5, high+0.5] given METAR-so-far.

    Used by the scalper. The logic:
      * If `current_max_f` is already above `bin_high + 0.5`, the bin is missed
        (already too hot). Return 0.
      * If `current_max_f >= bin_low - 0.5` and `hours_remaining` is small, the
        bin is *near-locked*: the high won't drop, and the only way to leave the
        bin is for some remaining hour to spike above `bin_high + 0.5`.
        Approximate that tail using a Normal(0, hourly_rise_std_f * sqrt(hours))
        on the *additional* rise above current.
      * If `current_max_f < bin_low - 0.5`, the bin is reachable only if the
        remaining hours produce enough rise — approximate with the same Normal.
    """
    bin_lo, bin_hi = bin_low_f - 0.5, bin_high_f + 0.5
    if current_max_f >= bin_hi:
        return 0.0
    sigma = max(hourly_rise_std_f * (max(hours_remaining, 0.1) ** 0.5), 0.5)
    p_below_top = _phi((bin_hi - current_max_f) / sigma)
    needed_rise = max(0.0, bin_lo - current_max_f)
    p_above_bottom = 1.0 - _phi((needed_rise) / sigma) if needed_rise > 0 else 1.0
    return max(0.0, min(1.0, p_below_top - (1.0 - p_above_bottom)))


def calibration_weights(
    sources: list[str], city: str, calibration: dict[tuple[str, str], tuple[int, float]], min_n: int
) -> dict[str, float]:
    """Convert Brier sums into ensemble weights (inverse-Brier).

    For sources with fewer than `min_n` settled observations we use uniform
    weight so we don't penalize new sources prematurely.
    """
    weights: dict[str, float] = {}
    for src in sources:
        n, brier_sum = calibration.get((src, city), (0, 0.0))
        if n < min_n or n == 0:
            weights[src] = 1.0
        else:
            avg_brier = brier_sum / n
            weights[src] = 1.0 / (avg_brier + 0.01)
    return weights


def load_calibration() -> dict[tuple[str, str], tuple[int, float]]:
    """Load the entire forecast_calibration table into a (source, city) -> (n, sum) dict."""
    from db import ForecastCalibration, get_session

    session = get_session()
    try:
        rows = session.query(ForecastCalibration).all()
        return {(r.source, r.city): (r.n or 0, r.brier_sum or 0.0) for r in rows}
    finally:
        session.close()


def update_calibration(source: str, city: str, predicted: float, outcome: int) -> None:
    """Add a single (predicted, outcome) Brier observation. outcome ∈ {0, 1}."""
    from db import ForecastCalibration, get_session

    delta = (predicted - outcome) ** 2
    session = get_session()
    try:
        row = (
            session.query(ForecastCalibration)
            .filter_by(source=source, city=city)
            .first()
        )
        if not row:
            row = ForecastCalibration(source=source, city=city, n=0, brier_sum=0.0)
            session.add(row)
        row.n = (row.n or 0) + 1
        row.brier_sum = (row.brier_sum or 0.0) + delta
        row.last_updated = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()


def persist_forecast(forecast: WeatherForecast) -> None:
    """Save a forecast snapshot to the DB so we can replay calibration later."""
    from db import WeatherForecastSnapshot, get_session

    session = get_session()
    try:
        session.add(
            WeatherForecastSnapshot(
                source=forecast.source,
                city=forecast.city,
                target_date=forecast.target_date,
                mean_f=forecast.mean_f,
                std_f=forecast.std_f,
                raw_json=json.dumps(forecast.raw)[:8000] if forecast.raw else None,
            )
        )
        session.commit()
    finally:
        session.close()


# --- Convenience: gather all configured sources for a city/date in parallel ---


async def gather_forecasts(
    client: httpx.AsyncClient,
    sources: list[str],
    city: str,
    target_date: str,
    cache_ttl: int,
) -> list[WeatherForecast]:
    tasks = [fetch_forecast(client, s, city, target_date, cache_ttl) for s in sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[WeatherForecast] = []
    for r in results:
        if isinstance(r, WeatherForecast):
            out.append(r)
        elif isinstance(r, Exception):
            log.warning(f"weather source error for {city} {target_date}: {r}")
    return out


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
