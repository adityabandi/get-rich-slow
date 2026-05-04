"""METAR live observation ingest from aviationweather.gov.

Plays the role for the scalper that ESPN plays for Kalshi: pulls live
ground-truth data (current temperature) and lets the scanner decide
"is this bin locked given what we already know?"

Endpoint: https://aviationweather.gov/api/data/metar?ids=...&format=json
Free, no auth, ~1 req/s rate limit. Returns latest observation per station.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from db import MetarObservation, get_session
from weather import CITY_COORDS

log = logging.getLogger(__name__)


@dataclass
class Metar:
    icao: str
    observed_at: datetime
    temp_c: float | None
    temp_f: float | None
    raw: str


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


async def fetch_metar(client: httpx.AsyncClient, icao: str) -> Metar | None:
    """Fetch the latest METAR for a single ICAO station."""
    try:
        resp = await client.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": icao, "format": "json", "taf": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning(f"metar fetch failed for {icao}: {e}")
        return None

    if not isinstance(data, list) or not data:
        return None
    obs = data[0]

    temp_c = obs.get("temp")
    if isinstance(temp_c, (int, float)):
        temp_c_val: float | None = float(temp_c)
    else:
        temp_c_val = None
    temp_f = c_to_f(temp_c_val) if temp_c_val is not None else None

    obs_time_raw = obs.get("obsTime") or obs.get("reportTime")
    try:
        if isinstance(obs_time_raw, (int, float)):
            observed_at = datetime.fromtimestamp(int(obs_time_raw), tz=timezone.utc)
        else:
            observed_at = datetime.fromisoformat(
                str(obs_time_raw).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
    except (TypeError, ValueError):
        observed_at = datetime.now(timezone.utc)

    return Metar(
        icao=icao,
        observed_at=observed_at,
        temp_c=temp_c_val,
        temp_f=temp_f,
        raw=str(obs.get("rawOb") or ""),
    )


async def fetch_all_configured(client: httpx.AsyncClient, cities: list[str]) -> list[Metar]:
    """Fetch METARs for all configured cities (serially — aviationweather is rate-limited)."""
    out: list[Metar] = []
    for slug in cities:
        coords = CITY_COORDS.get(slug)
        if not coords:
            continue
        m = await fetch_metar(client, coords["icao"])
        if m and m.temp_f is not None:
            out.append(m)
        # Soft 1 req/s to stay polite
        await asyncio.sleep(1.1)
    return out


def persist_observation(metar: Metar) -> None:
    """Upsert one METAR observation. Idempotent on (icao, observed_at)."""
    session = get_session()
    try:
        existing = (
            session.query(MetarObservation)
            .filter_by(icao=metar.icao, observed_at=metar.observed_at)
            .first()
        )
        if existing is None and metar.temp_f is not None:
            session.add(
                MetarObservation(
                    icao=metar.icao,
                    observed_at=metar.observed_at,
                    temp_f=metar.temp_f,
                    raw=metar.raw[:500] if metar.raw else None,
                )
            )
            session.commit()
    finally:
        session.close()


def max_observed_today(icao: str, target_date: str) -> float | None:
    """Highest temp_f observed for a station on `target_date` (UTC date string)."""
    from sqlalchemy import func

    session = get_session()
    try:
        result = (
            session.query(func.max(MetarObservation.temp_f))
            .filter(
                MetarObservation.icao == icao,
                func.date(MetarObservation.observed_at) == target_date,
            )
            .scalar()
        )
        return float(result) if result is not None else None
    finally:
        session.close()


def latest_for_station(icao: str) -> MetarObservation | None:
    """Most recent observation for a station (for the scalper's locked-bin check)."""
    session = get_session()
    try:
        return (
            session.query(MetarObservation)
            .filter(MetarObservation.icao == icao)
            .order_by(MetarObservation.observed_at.desc())
            .first()
        )
    finally:
        session.close()
