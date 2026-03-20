# Railway Deployment Guide

## Prerequisites
- [Railway account](https://railway.app) (sign up with GitHub)
- [Kalshi account](https://kalshi.com/signup) with API keys
- This repo pushed to your GitHub

## Step 1: Create Railway Project

1. Go to [railway.app/new](https://railway.app/new)
2. Click **"Deploy from GitHub Repo"**
3. Select `adityabandi/get-rich-slow`
4. Railway will detect the Dockerfile and start building

## Step 2: Add Persistent Volume

The scanner uses SQLite — without a volume, your DB resets on every deploy.

1. In your Railway project, click **"+ New"** → **"Volume"**
2. Set mount path to `/data`
3. Size: 1 GB is more than enough (~$0.25/month)

## Step 3: Set Environment Variables

In Railway dashboard → your service → **Variables** tab, add:

```
KALSHI_API_KEY=your-kalshi-api-key-id
KALSHI_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----
API_TOKEN=pick-any-strong-random-string
DRY_RUN=true
MIN_YES_PRICE=90
MAX_BET_AMOUNT_CENTS=300
POLL_INTERVAL_SECONDS=30
PORT=8000
```

### Getting your Kalshi API Key:
1. Log into [kalshi.com](https://kalshi.com)
2. Go to Settings → API Keys
3. Generate a new API key — save the Key ID
4. Generate an RSA private key — save the PEM content
5. When pasting the private key in Railway, replace newlines with `\n`

### Note on KALSHI_PRIVATE_KEY format:
Railway env vars don't support multi-line values well. Convert your PEM to a single line:
```bash
cat your-key.pem | tr '\n' '\\n' | sed 's/\\n$//'
```

## Step 4: Deploy

Railway auto-deploys on push. Once the build completes:

1. Check **Deployments** tab for build logs
2. Click the generated URL to see the health check (`{"status": "ok"}`)
3. Check logs — you should see ESPN polling and Kalshi scanning

## Step 5: Monitor

### Check the API:
```bash
# Health check
curl https://your-app.railway.app/

# View stats
curl https://your-app.railway.app/api/stats \
  -H "Authorization: Bearer YOUR_API_TOKEN"

# View live games being tracked
curl https://your-app.railway.app/api/live-games

# View config
curl https://your-app.railway.app/api/config \
  -H "Authorization: Bearer YOUR_API_TOKEN"

# View trades
curl https://your-app.railway.app/api/trades \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Watch logs:
Railway dashboard → your service → **Logs** tab. You should see:
- `ESPN: refreshing live game state...` every 10 seconds
- `Kalshi: scanning for Yes >= 90c...` every 5 seconds
- Opportunities logged when games are in final minutes

## Step 6: Go Live (when ready)

After watching dry-run for a few days and seeing the scanner find good opportunities:

1. In Railway Variables, change: `DRY_RUN=false`
2. Railway will auto-redeploy
3. The scanner will now place real orders on Kalshi

## Budget-Aware Defaults

These defaults are tuned for a ~$200 starting balance:

| Setting | Value | Why |
|---------|-------|-----|
| `MIN_YES_PRICE` | 90 | Only buy at 90¢+ (safer, smaller spread) |
| `MAX_BET_AMOUNT_CENTS` | 300 | Max $3 per bet (~3 contracts at 92¢) |
| `max_positions` | 10 | Max 10 simultaneous bets ($30 max exposure) |

## Adjusting Config at Runtime

You can change settings without redeploying:

```bash
# Lower the min price threshold
curl -X PUT https://your-app.railway.app/api/config \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "min_yes_price", "value": "88"}'

# Increase max bet size
curl -X PUT https://your-app.railway.app/api/config \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "max_bet_cents", "value": "500"}'
```

## Costs

- **Railway**: ~$5/month (Hobby plan) + $0.25/month (1GB volume)
- **Kalshi**: Fees on settlement (~1-2¢ per contract)
- **Total overhead**: ~$5.25/month
