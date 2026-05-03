# Base44 Superagent — oversight layer for the Polymarket weather bot

The Python scanner (`scanner.py` + `weather_scanner.py`) is the trading core. Don't move that into Base44 — it's a 10-second async loop with a websocket listener, wrong shape for an agent runtime.

The right split:

| Layer | Where it lives | Job |
|---|---|---|
| Trading core | Python on ECS (this repo) | Scan markets, compute EV, place / settle trades, write Claude lessons |
| Oversight | **Base44 Superagent** | Read `/api/weather-*`, alert you on phone, approve config tweaks |

Superagents (per [base44.com/superagents](https://base44.com/superagents)) is "always on", supports cron / event triggers, can call APIs or MCPs, and lives in WhatsApp / Telegram / Slack / browser. Perfect fit for the oversight role.

## API surface the Superagent should hit

Base URL: `https://getrich-api.rager.tech`

All endpoints accept `Authorization: Bearer $API_TOKEN` (set via `npx sst secret set ApiToken …`).

| Endpoint | Method | Use it for |
|---|---|---|
| `/api/weather-trades?limit=50` | GET | Recent trades, including settled ones with P&L |
| `/api/weather-markets?limit=50` | GET | What markets the scanner is watching right now |
| `/api/weather-lessons?status=pending` | GET | Claude-written post-settlement lessons awaiting your review |
| `/api/weather-lessons/{id}/approval` | POST | `{"approval_status": "approved", "apply_config_change": true}` |
| `/api/weather-calibration` | GET | Per-(source, city) Brier scores — which forecast source is best |
| `/api/config` | PUT | `{"key": "weather_min_ev", "value": "0.12"}` to tune any knob |
| `/api/stats` | GET | Account-wide stats (P&L, win rate, balance) |

## Paste-ready Superagent prompts

These follow the "give your Superagent a job" format Base44 uses on its landing page.

### 1. Daily summary at 8am

> Every morning at 8am, GET `https://getrich-api.rager.tech/api/weather-trades?limit=50` with header `Authorization: Bearer <API_TOKEN>`. From the response, count yesterday's settled trades (status = settled_win or settled_loss), sum the pnl_usdc, and list the worst loser. Also GET `/api/weather-lessons?status=pending&limit=5` and include any pending Claude suggestions. Send the summary to my Telegram. Keep it under 6 lines.

### 2. Approval queue — push pending lessons to my phone

> Every 4 hours, GET `https://getrich-api.rager.tech/api/weather-lessons?status=pending&limit=10` with my Bearer token. For each lesson where `suggested_config_key` is not null, send me a Telegram message: "[city] [target_date]: [lesson]. Suggested tweak: [key]=[value]. Approve? (reply Y/N with id [id])". When I reply Y to an id, POST `/api/weather-lessons/{id}/approval` with body `{"approval_status": "approved", "apply_config_change": true}`. When I reply N, post the same with `"approval_status": "rejected"`.

### 3. Calibration alert — flag a degrading forecast source

> Every 6 hours, GET `https://getrich-api.rager.tech/api/weather-calibration` with my Bearer token. If any (source, city) row has `n >= 20` and `brier_avg > 0.3`, alert me on Telegram with the source name, city, and Brier score. Remember which (source, city) you've already alerted on this week so you don't spam me — only re-alert if the Brier crosses 0.4 or it's been 7 days.

### 4. P&L week-in-review on Sunday

> Every Sunday at 5pm, GET `/api/weather-trades?limit=200`. Filter to trades that settled in the last 7 days. Compute: total trades, win rate, summed pnl_usdc, biggest winner, biggest loser, and which city had the best win rate. Send a Slack message to #weather-bot with the breakdown.

### 5. Stop-loss override — pause the bot if losses pile up

> Every hour, GET `/api/weather-trades?limit=50&include_dry=false`. If the sum of pnl_usdc across trades settled in the last 24 hours is below -$10, PUT `/api/config` with `{"key": "weather_enabled", "value": "false"}` and Telegram me "Weather bot paused — yesterday's P&L was [amount]. Restart with /resume." When I message /resume, PUT it back to "true".

## Notes

- **The Superagent is always on; the trading bot is too.** They're independent — if Base44 goes down, the scanner keeps trading. If the scanner crashes, Superagent surfaces the gap (pending lessons stop arriving).
- **Don't put real credentials in prompts.** Base44 has its own secrets store — set `API_TOKEN` there and let it interpolate.
- **`apply_config_change=true` is gated by Bearer auth.** Anyone with the token can change config; rotate the token if the Superagent's cred is ever exposed.
- **Web3 / live Polymarket trading still needs Python.** Superagents can't sign EIP-712 orders against the Polygon CLOB. When you flip `weather_live=true`, the order signing happens in `polymarket_client.place_order` (currently stubbed) — Superagents only sees the placed trade after the fact.
