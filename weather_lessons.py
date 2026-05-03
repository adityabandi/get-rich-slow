"""Post-settlement lesson writer powered by the Anthropic SDK.

After each Polymarket weather trade settles, ask Claude to write a 1–2 sentence
lesson that captures whether the model called it correctly and why. Cheap by
design — uses Haiku 4.5 with no thinking and a tight max_tokens, ~$0.001 per
settlement. Lessons live in the `weather_lessons` table and are surfaced via
`/api/weather-lessons` (consumed by the dashboard or Base44).

Optional config knobs (DB-backed, see db.py `_CONFIG_DEFAULTS`):
- `weather_lessons_enabled` — master switch (default true)
- `weather_lessons_model`   — Claude model ID (default `claude-haiku-4-5`)
- `weather_lessons_max_tokens` — response cap (default 200)

Failure modes are non-fatal: missing ANTHROPIC_API_KEY → skip silently; SDK
error → log warning and persist a placeholder row. Settlement never blocks on
lesson generation.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import anthropic

from db import (
    ForecastCalibration,
    WeatherLesson,
    WeatherTrade,
    get_config,
    get_config_bool,
    get_config_int,
    get_session,
)

log = logging.getLogger(__name__)


# Frozen system prompt — keep stable so prompt-cache could kick in if the
# context ever grows past ~4k tokens. Today it's ~150 tokens which is below
# Haiku's 4096-token caching minimum, so caching is a no-op for now.
_SYSTEM_PROMPT = """You are a post-trade analyst for a Polymarket weather \
trading bot. The bot trades YES on temperature-bin markets ("high in Chicago \
between 82 and 83°F on D+1") when its multi-source ensemble forecast says EV \
> threshold.

For each settled trade, write ONE lesson — strictly 1 to 2 sentences. Cover:
1. Did the model call this correctly, given the actual outcome?
2. The most likely cause of any miss (forecast variance, regime change, \
under/over-weighted source).
3. One concrete suggestion ONLY if the data clearly warrants it.

Then OPTIONALLY suggest a single config tweak in the structured JSON section. \
Only suggest one if the trade reveals a clear, actionable pattern — never \
suggest a tweak after a single trade just to fill the field.

Be terse. No preamble. No emoji."""


_CLIENT: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic | None:
    """Lazy-initialize the Anthropic client; return None if no key configured."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    _CLIENT = anthropic.Anthropic(api_key=api_key)
    return _CLIENT


def _trade_summary(trade: WeatherTrade) -> str:
    """Render a compact, deterministic per-trade summary for the model."""
    p = trade.pnl_usdc or 0
    pnl = f"+${p:.2f}" if p >= 0 else f"-${abs(p):.2f}"
    return (
        f"city={trade.city} target_date={trade.target_date} "
        f"bin={trade.low_f:.0f}-{trade.high_f:.0f}F "
        f"yes_price={trade.yes_price:.3f} model_p={trade.p_model:.3f} ev={trade.ev:.3f} "
        f"size_usdc={trade.size_usdc:.2f} actual_high={trade.actual_high_f}F "
        f"status={trade.status} pnl={pnl}"
    )


def _calibration_snapshot(city: str | None) -> str:
    """Brief calibration context so Claude can spot per-source patterns."""
    if not city:
        return "calibration: (no city)"
    session = get_session()
    try:
        rows = session.query(ForecastCalibration).filter_by(city=city).all()
        if not rows:
            return f"calibration({city}): no settled history yet"
        parts = []
        for r in rows:
            avg = (r.brier_sum / r.n) if r.n else None
            if avg is not None:
                parts.append(f"{r.source}: n={r.n} brier={avg:.3f}")
            else:
                parts.append(f"{r.source}: n=0")
        return f"calibration({city}): " + ", ".join(parts)
    finally:
        session.close()


def generate_lesson(trade: WeatherTrade) -> WeatherLesson | None:
    """Generate and persist a lesson for one settled trade.

    Returns the persisted WeatherLesson row, or None if the lesson writer is
    disabled / the API key is missing / the API call failed. Never raises.
    """
    if not get_config_bool("weather_lessons_enabled"):
        return None

    client = _client()
    if client is None:
        log.debug("weather lessons: ANTHROPIC_API_KEY not set; skipping")
        return None

    model = get_config("weather_lessons_model") or "claude-haiku-4-5"
    max_tokens = get_config_int("weather_lessons_max_tokens") or 200

    user_prompt = (
        f"{_trade_summary(trade)}\n"
        f"{_calibration_snapshot(trade.city)}\n\n"
        "Return your response as JSON with this exact shape and nothing else:\n"
        '{"lesson": "<1-2 sentences>", '
        '"suggested_config_key": "<key or null>", '
        '"suggested_config_value": "<value or null>"}\n\n'
        "Use null (not empty string) when no config tweak is warranted."
    )

    lesson_text = ""
    suggested_key: str | None = None
    suggested_value: str | None = None
    input_tokens = 0
    output_tokens = 0

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            "",
        )
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        try:
            parsed = json.loads(text.strip())
            lesson_text = str(parsed.get("lesson") or "").strip()
            sk = parsed.get("suggested_config_key")
            sv = parsed.get("suggested_config_value")
            suggested_key = str(sk) if sk not in (None, "", "null") else None
            suggested_value = str(sv) if sv not in (None, "", "null") else None
        except (ValueError, AttributeError):
            # Model didn't return valid JSON — fall back to using the raw text
            # as the lesson so the human review still gets something useful.
            lesson_text = text.strip()[:500]

    except anthropic.AuthenticationError:
        log.warning("weather lessons: invalid ANTHROPIC_API_KEY")
        return None
    except anthropic.RateLimitError as e:
        log.warning(f"weather lessons: rate limited ({e}); skipping this trade")
        return None
    except anthropic.APIError as e:
        log.warning(f"weather lessons: API error ({e}); skipping this trade")
        return None

    if not lesson_text:
        return None

    session = get_session()
    try:
        row = WeatherLesson(
            trade_id=trade.id,
            city=trade.city,
            target_date=trade.target_date,
            lesson=lesson_text,
            suggested_config_key=suggested_key,
            suggested_config_value=suggested_value,
            approval_status="pending",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    finally:
        session.close()
