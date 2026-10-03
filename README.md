# Equination

Daily NSE stock picks with an entry, a stop, a target and a live track record.

Equination is a self-hostable, multi-user web app. Each user connects their
own **Upstox** API key (prices, key ratios, shareholding) and optionally a
**Marketaux** token (news sentiment). Every trading day it scans ~200 liquid
NSE stocks for one setup, gates on fundamentals, promoter behaviour and news,
and grades its own past picks. It never places orders.

![app](docs/dashboard.png)

## Features

- **4-day swing mode** (default): quality stock in an uptrend, 3–12% pullback,
  RSI(2) oversold, then a bullish reversal candle (morning star, engulfing,
  piercing, hammer, harami or strong high-volume close). Entry, stop under the
  pattern low, target at the 10-day high or 2R, hold-until date, 1%-risk sizing.
- **Positional mode**: monthly momentum rotation with a Nifty 200-DMA regime filter.
- **Gates**: ROE, P/E, D/E, margins (Upstox key ratios); promoters cutting
  stake or pledging (Upstox shareholding); user exclude list.
- **Three views**: *Quant* (numbers only), *Sentiment* (news only, plus a
  market-wide sweep of most positive / negative names), *Cumulative* (weighted blend).
- **Backtest** of the rules on the user's downloaded history, and a
  **Performance** tab that forward-grades every pick ever made.
- **Accounts**: email + password (scrypt), server-side sessions, per-user
  settings, scans, schedule; secrets encrypted at rest; shared market-data
  caches so API quotas stretch across users.

## Run locally

```bash
pip install -r requirements.txt
python run.py            # http://127.0.0.1:8000
```

Open the landing page, create an account, then in **Settings → Upstox** paste
the key and secret of an app you created at `account.upstox.com/developer/apps`
with redirect URL `http://127.0.0.1:8000/callback`, click **Connect Upstox**,
and run a scan. Add a Marketaux token to enable sentiment.

## Deploy (public, multi-user)

Equination is a long-running server with a SQLite database on disk, so it
needs a host with a persistent volume — Fly.io, Railway, Render (paid disk) or
a VPS. **Serverless hosts such as Vercel will not work.**

```bash
# Fly.io example
fly launch --copy-config --no-deploy
fly volumes create equination_data -s 1 -r bom
fly secrets set EQUINATION_SECRET=$(openssl rand -hex 32) \
                EQUINATION_PUBLIC_URL=https://<app>.fly.dev \
                EQUINATION_INVITE_CODE=<optional-beta-code>
fly deploy
```

| Variable | Default | Meaning |
|---|---|---|
| `EQUINATION_SECRET` | auto-generated key file in the data dir | Master secret for encrypting users' API secrets and tokens. **Set it explicitly in production** and never change it afterwards (stored secrets become unreadable). |
| `EQUINATION_PUBLIC_URL` | `http://127.0.0.1:8000` | Public base URL; used for the default Upstox redirect URI (`<url>/callback`) and secure cookies. |
| `EQUINATION_ALLOW_SIGNUP` | `1` | Set `0` to close sign-ups. |
| `EQUINATION_INVITE_CODE` | unset | When set, sign-up requires this code. Good for a private beta. |
| `EQUINATION_DATA_DIR` | `./data` | SQLite + key file location (mount a volume here). |
| `EQUINATION_HOST` / `EQUINATION_PORT` | `127.0.0.1` / `8000` | Bind address; `PORT` from the host is honoured. |
| `EQUINATION_RPS` | `1.1` | Requests/second to Upstox (their 2000 per 30 min cap is the binding one). |
| `EQUINATION_HISTORY_DAYS` | `2190` | Days of history on first download. |

Each user registers their **own** Upstox developer app with redirect URL
`https://<your-domain>/callback`. Upstox tokens expire at 03:30 IST daily, so
users reconnect once a day; a scan that hits an expired token is marked
`needs_login` and the UI says so.

Scans run on a 2-worker pool; the candle cache is shared, so after the first
user's scan of the day the others are nearly free. Upstox fundamentals are
cached 7 days and Marketaux news 12 hours per symbol, across users.

`/healthz` returns `{"ok": true}` for load-balancer checks. There is no admin
UI; the SQLite file is the source of truth.

## Security notes

- Passwords: scrypt with per-user salt. Sessions: random 256-bit tokens,
  HttpOnly cookies, `Secure` when `EQUINATION_PUBLIC_URL` is https.
- Secrets: HMAC-SHA256 counter-mode keystream + HMAC tag (encrypt-then-MAC)
  with keys derived from `EQUINATION_SECRET`; stdlib only.
- Secrets are never returned by the API; the UI only learns whether one is set.
- Put the app behind TLS (Fly/Railway do this for you).

## Strategy details

See the **Strategy** tab in the app, or the docstrings in `app/swing.py`,
`app/strategy.py`, `app/fundamentals.py` and `app/sentiment.py`.

## Layout

```
app/
  main.py           routes: landing/auth pages, per-user JSON API, OAuth callback
  auth.py           passwords, sessions, secret encryption
  db.py             SQLite: users, sessions, settings, scans + shared caches
  scanner.py        per-user scan pipeline on a worker pool
  scheduler.py      per-user daily cron (IST)
  strategy.py       positional mode
  swing.py          swing mode + event backtest
  patterns.py       candlestick patterns
  fundamentals.py   Upstox ratios + shareholding gates
  sentiment.py      Marketaux sentiment + market sweep
  performance.py    forward-grading of past picks
static/             landing.html, auth.html, legal.html, app.html, app.js, styles.css
tests/              python -m pytest
```

## Disclaimer

Educational software, not investment advice. No strategy earns a fixed
monthly return; size positions so that a 20–30% drawdown is survivable.
