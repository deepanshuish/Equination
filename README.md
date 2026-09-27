# Equination

A self-hosted web app that pulls NSE daily prices through **your own Upstox API
key**, runs a trend-filtered momentum strategy every trading day, and shows
you which stocks to hold, with stop-losses and position sizes.

It is a **positional** (not intraday) system that aims for a realistic
1-3% per month on average, with losing months. It never places orders.

![dashboard](docs/dashboard.png)

## Quick start

```bash
pip install -r requirements.txt
python run.py            # http://127.0.0.1:8000
```

1. Create an app at `account.upstox.com/developer/apps`. Set its **redirect
   URL** to `http://127.0.0.1:8000/callback`.
2. Open **Settings** in Equination, paste the API key and secret, click
   **Login with Upstox**.
3. Back on the **Dashboard**, click **Run scan now**. The first run downloads
   ~6 years of daily candles for ~200 stocks (about 3-4 minutes under Upstox's
   rate limits); later runs only fetch the missing days.
4. Enable the daily schedule (default 18:30 IST, Mon-Fri). Upstox access
   tokens expire at 03:30 IST every day, so log in once a day before the scan
   runs, or paste a token manually. A scan that finds an expired token is
   marked `needs_login` and the dashboard tells you.
5. Open **Backtest** to see how the exact same rules did on the data you just
   downloaded, versus buying the Nifty 50.

Everything (settings, secret, token, candles, scan history) lives in a local
SQLite file under `data/`. Nothing leaves your machine except calls to Upstox.

## The strategy

**Trend-filtered, volatility-adjusted momentum rotation**, see the *Strategy*
tab in the app or `app/strategy.py`.

| Step | Rule |
|---|---|
| Regime | Nifty 50 above 50 & 200 DMA → full allocation; above 200 only → half; below 200 → cash |
| Eligibility | ≥ 260 bars, price ≥ ₹50, 20-day turnover ≥ ₹5 cr, above own 50 & 200 DMA, within 25% of 52-week high, ann. vol ≤ 60%, last-month return ≤ 35% |
| Score | `(0.40·R12-1 + 0.35·R6 + 0.25·R3) / vol63` |
| Portfolio | Top 10 by score, equal slots, quantity capped by 1% risk to a 2.5·ATR stop |
| Exits | Close below stop or 50 DMA, or falls out of the top 2N at the monthly review |

Why momentum: it is the most persistent, best-documented return anomaly across
markets including India. Its known weakness (crashes when the market turns) is
what the Nifty regime filter and volatility scaling are there to blunt.
Run the backtest on your own data before trusting it; past performance does
not guarantee future results.

## Deploying (not Vercel)

Equination is a long-running server: it keeps a SQLite database on disk,
runs multi-minute scans in a background thread and hosts its own daily
scheduler. **Serverless platforms such as Vercel or Netlify cannot run it**
(read-only filesystem, short timeouts, no background jobs → `FUNCTION_INVOCATION_FAILED`).

Run it on your own PC, or on any host with a persistent disk using the
included `Dockerfile`:

| Host | Notes |
|---|---|
| Your PC | `python run.py`; keep it on at scan time |
| Fly.io | `fly launch --copy-config --no-deploy`, `fly volumes create equination_data -s 1 -r bom`, `fly secrets set EQUINATION_PASSWORD=… EQUINATION_PUBLIC_URL=https://<app>.fly.dev`, `fly deploy` |
| Railway | New project from repo, add a **Volume** mounted at `/data`, set the env vars below |
| Any VPS | `docker build -t equination . && docker run -d -p 8000:8000 -v equination:/data -e EQUINATION_PASSWORD=… equination` |

When deployed, set these and register `EQUINATION_PUBLIC_URL/callback` as the
redirect URL of your Upstox app:

- `EQUINATION_PASSWORD` — **required** once the app is reachable from the
  internet; it stores your Upstox secret and token. The browser will prompt for
  it (any username).
- `EQUINATION_PUBLIC_URL` — e.g. `https://equination.fly.dev`; sets the default
  OAuth redirect URI.
- `EQUINATION_DATA_DIR=/data` — the mounted volume (already set in the Dockerfile).

## Configuration

Environment variables (all optional):

| Var | Default | Meaning |
|---|---|---|
| `EQUINATION_HOST` / `EQUINATION_PORT` | `127.0.0.1` / `8000` | Bind address (`PORT` from the host is honoured too) |
| `EQUINATION_PASSWORD` | unset | HTTP Basic-auth password for every page and API call |
| `EQUINATION_PUBLIC_URL` | `http://host:port` | Base URL used for the default OAuth redirect URI |
| `EQUINATION_DATA_DIR` | `./data` | Where the SQLite database lives |
| `EQUINATION_RPS` | `1.1` | Requests per second to Upstox (their 2000 / 30 min cap is the binding one) |
| `EQUINATION_HISTORY_DAYS` | `2190` | Days of history to download on first run |

Universe: `curated` (~200 liquid large and mid caps, `app/universe.py`) or
`nse_all` (every NSE cash equity; first download takes ~30 minutes).

## Layout

```
app/
  main.py           FastAPI routes + OAuth callback
  upstox_client.py  login, instrument master, daily candles (v3 with v2 fallback)
  scanner.py        daily pipeline: refresh data → score → persist
  strategy.py       regime filter, features, ranking, position sizing
  backtest.py       monthly-rebalance backtest on the cached candles
  scheduler.py      APScheduler cron (IST)
  db.py             SQLite storage
static/             single-page UI (no build step)
tests/              synthetic-data tests: python -m pytest
```

## Disclaimer

Educational software. It is not investment advice, and no strategy earns a
fixed monthly return. Size positions so that a 20-30% drawdown is survivable.
