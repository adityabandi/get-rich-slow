# Predictions Project

Kalshi sports prediction market scanner. Buys YES contracts at 88-99c on games that are nearly decided, collects $1 at settlement.

## Stack
- **Python**: FastAPI + SQLAlchemy + SQLite (api.py, scanner.py, espn.py, db.py)
- **Dashboard**: Next.js 16 (dashboard/)
- **Infra**: SST v3 on AWS (sst.config.ts) — ECS for API, Lambda/CloudFront for dashboard, EFS for SQLite

## Tooling
- **Python**: uv (not pip), ruff (not black), ty (not mypy)
- **JS/TS**: pnpm, oxfmt (4 spaces), oxlint (not eslint)
- **No dev server**: Use Docker for everything locally
- **CLI first**: Use `pnpm cli` (react-ink TUI) for config changes, stats, and trades whenever possible instead of direct API calls or DB access

## Security
- **NEVER commit secrets**: API tokens, Kalshi keys, Cloudflare tokens, passwords, private keys must never appear in code or config files. Use `$API_TOKEN`, `$KALSHI_API_KEY` placeholders in docs.
- **Before every commit**: Scan staged changes for secrets (API keys, tokens, passwords, private keys). If found, abort and fix.
- **Secrets live in**: SST secrets (`npx sst secret set`) and `.envrc` (gitignored). Never in code.

## Deploy
```bash
# Deploy to AWS (requires `assume smooai.dev` for AWS auth)
pnpm sst:deploy

# Or manually:
rm -rf dashboard/.open-next dashboard/.next && AWS_PROFILE=smooai.dev npx sst deploy
```

## Runtime Config (Tunable Parameters)

Config is stored in SQLite (`config` table) and read by the scanner every loop iteration. Changes take effect within 5 seconds without redeploying.

### View current config
```bash
uv run python config_cli.py
```

### Update config via CLI (local — requires DB access)
```bash
uv run python config_cli.py set KEY VALUE
uv run python config_cli.py delete KEY     # revert to default
```

### Update config via API (remote — works from anywhere)
```bash
curl -X PUT https://getrich-api.rager.tech/api/config \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "min_yes_price", "value": "88"}'
```

### Available config keys

**Trading parameters:**
| Key | Default | Description |
|-----|---------|-------------|
| `min_yes_price` | 88 | Minimum YES ask price in cents to place a bet |
| `max_bet_cents` | 500 | Maximum cost per bet in cents |
| `max_positions` | 30 | Maximum concurrent open positions |
| `min_volume` | 100 | Minimum market volume for liquidity |
| `stretch_price_min` | 85 | Minimum YES price for stretch (shadow) tracking |
| `max_daily_loss` | 2000 | Daily loss limit in cents before pausing new bets |
| `max_portfolio_exposure_pct` | 67 | Max % of daily balance committed at once |

**Per-sport score leads** (`lead:{sport_path}`):
| Key | Default | Description |
|-----|---------|-------------|
| `lead:basketball/nba` | 15 | Min point lead for NBA |
| `lead:basketball/mens-college-basketball` | 15 | Min point lead for NCAAMB |
| `lead:hockey/nhl` | 3 | Min goal lead for NHL |
| `lead:football/nfl` | 17 | Min point lead for NFL |
| `lead:football/college-football` | 17 | Min point lead for NCAAFB |
| `lead:baseball/mlb` | 4 | Min run lead for MLB |
| `lead:soccer/eng.1` | 2 | Min goal lead for EPL |
| `lead:soccer/esp.1` | 2 | Min goal lead for La Liga |
| `lead:soccer/usa.1` | 2 | Min goal lead for MLS |

**Per-sport end-of-game timing** (`final_seconds:{sport_path}`):
| Key | Default | Description |
|-----|---------|-------------|
| `final_seconds:basketball/nba` | 180 | Clock <= 3:00 in final quarter |
| `final_seconds:hockey/nhl` | 180 | Clock <= 3:00 in final period |
| `final_seconds:football/nfl` | 180 | Clock <= 3:00 in 4th quarter |
| `final_seconds:soccer/eng.1` | 4800 | Clock >= 80th minute |
| `final_seconds:soccer/esp.1` | 4800 | Clock >= 80th minute |
| `final_seconds:soccer/usa.1` | 4800 | Clock >= 80th minute |

Note: For countdown sports (NBA, NHL, NFL, etc.) the value means "clock must be <= X seconds". For count-up sports (soccer) it means "clock must be >= X seconds".

**Polymarket weather bot** (parallel module — runs alongside Kalshi sports loop, dry-run only in v1):
| Key | Default | Description |
|-----|---------|-------------|
| `weather_enabled` | `false` | Master switch — set `true` to start the weather loop |
| `weather_live` | `false` | Live trading switch — `false` keeps the loop in dry-run only |
| `weather_min_ev` | `0.10` | Min expected value per $1 of YES bought |
| `weather_min_volume` | `2000` | Min market volume (USDC) for liquidity |
| `weather_max_price` | `0.45` | Don't buy YES at probabilities higher than this |
| `weather_max_bet_usdc` | `2.00` | Max $ per single weather trade |
| `weather_kelly_fraction` | `0.25` | Fractional Kelly multiplier |
| `weather_min_hours` | `2` | Min hours-to-resolution before trading |
| `weather_max_hours` | `72` | Max hours-to-resolution before trading |
| `weather_scan_interval_seconds` | `600` | How often the loop runs |
| `weather_sources` | `open-meteo,visual-crossing` | Forecast sources (CSV) |
| `weather_cities` | `new-york,chicago,...` | City slugs to scan (CSV; matches `weather.CITY_COORDS`) |
| `weather_calibration_min` | `10` | Settled trades per (source,city) before its weight applies |

Visual Crossing requires `VC_API_KEY` env var (SST secret `VisualCrossingKey`). Without it, only Open-Meteo is used.

## Key Files
- `sst.config.ts` — SST infra (VPC, ECS, EFS, S3, secrets)
- `scanner.py` — Main scanner with WebSocket + ESPN integration
- `api.py` — FastAPI backend serving dashboard + config endpoints
- `db.py` — SQLAlchemy models + config helpers
- `espn.py` — ESPN live game data
- `kalshi_client.py` — Kalshi REST + WebSocket client
- `config_cli.py` — CLI to view/update runtime config
- `polymarket_client.py` — Polymarket Gamma + CLOB read-only client (weather bot)
- `weather.py` — Forecast aggregation (Open-Meteo, Visual Crossing) + probability math
- `weather_scanner.py` — Polymarket weather scan loop + settlement/calibration
- `dashboard/` — Next.js app (read-only display)

## Architecture
- Scanner runs 5 concurrent async loops: ESPN poll (10s), Kalshi API scan (5s), WebSocket listener (real-time prices + settlements), DB backup (30m), Polymarket weather scan (10m, gated by `weather_enabled`)
- Config stored in SQLite `config` table, read each scan loop — changes take effect immediately
- Dashboard is read-only, all config changes go through CLI or Bearer-protected PUT endpoint
- Weather bot: pulls Polymarket weather markets ("high in Chicago 82–83°F"), combines forecast sources into a Normal(μ,σ²) over the daily high, computes EV vs market price, dry-run trades sized via fractional Kelly. Self-learning loop: per (source, city) Brier score updated on settlement and used as inverse-weight in the next ensemble.
