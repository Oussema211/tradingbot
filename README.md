# Momentum Desk

A momentum stock scanner (Ross Cameron style). It finds today's strongest small-cap movers, explains each one in plain English, gives you an exact plan (entry, max price, stop, target, share count), and **sends alerts to your phone**.
It never places orders. Education only, not financial advice.

**Everything here is free**: Alpaca data (IEX feed), Telegram or ntfy alerts, and the hosting options below.

---

## 1. Run it on your PC (2 minutes)

1. Install Python 3.10+ from python.org (tick **Add to PATH**).
2. Copy `.env.example` to `.env` and paste your Alpaca paper keys (free at alpaca.markets). *(Your current keys are already in `.env`.)*
3. Double-click **START.bat**. The dashboard opens at http://localhost:8765.
   `START_DEMO.bat` shows fake data so you can see everything outside market hours.

---

## 2. Phone alerts (free, pick one)

### Telegram (recommended)
1. In Telegram, open **@BotFather**, send `/newbot`, choose a name. Copy the **token** it gives you.
2. Paste it in `.env`: `TELEGRAM_BOT_TOKEN=123456:ABC...`
3. Open your new bot in Telegram and press **Start**.
4. Run `python momentum_scanner.py --telegram-setup`. It prints `TELEGRAM_CHAT_ID=...`. Paste that line into `.env`.
5. Run `python momentum_scanner.py --test-alert`. Your phone should buzz.

### ntfy (no account at all)
1. Install the **ntfy** app (Android / iPhone).
2. Pick a long random topic name, e.g. `desk-k39fh2qz81` (anyone who knows the name can read it, so make it hard to guess).
3. In the app, subscribe to that topic. In `.env`: `NTFY_TOPIC=desk-k39fh2qz81`
4. `python momentum_scanner.py --test-alert`

### Discord
Server settings → Integrations → Webhooks → New webhook → Copy URL → `DISCORD_WEBHOOK_URL=` in `.env`.

### What you'll receive
| When | Message |
|---|---|
| 09:00 NY | **Morning brief**: top gappers, market mood, early close warning |
| 09:25 | Market opens in 5 minutes |
| 09:30 | Trading window is open |
| Any time in the window | 🟡 **GET READY**: entry, max price, stop, target, share count, why, warnings |
| | 🟢 **BUY NOW / BUY SMALL**: same details, highest priority |
| | 🔴 **SETUP FAILED**: a stock you were alerted on fell below its stop |
| 11:00 | Window closed + summary of the day |
| Weekday holiday | Market closed today |
| Problems | Scanner hasn't had data for 5+ minutes / recovered |

There's no spam: a GET READY must hold for 2 scans in a row, each setup is sent once, and nothing is re-sent after a restart.
By default you only get BUY alerts and their follow-ups (SELL HALF, EXIT NOW, TARGET HIT). Set `ALERT_LEVEL=ready` to also get GET READY heads-ups.

---

## 3. Keep it running 24/7 for free

| Option | Cost | Dashboard online | Alerts | Needs a card | Effort |
|---|---|---|---|---|---|
| **A. Oracle Cloud Always Free** (recommended) | Free forever | ✅ | ✅ | Yes (verification only, never charged on the free tier) | 20 min |
| **B. GitHub Actions** | Free | ❌ (alerts only) | ✅ | No | 10 min |
| **C. Your own PC** | Free | On your PC | ✅ while the PC is on | No | 1 min |

> Google Cloud's free **e2-micro** VM (regions us-west1, us-central1, us-east1) works exactly like Option A if Oracle signup fails for you.
> Avoid "free web hosting" like Render or Koyeb free tiers: they put the app to sleep when nobody visits, so the scanner stops.

### Option A: Oracle Cloud Always Free (a real server that never sleeps)
1. Sign up at **cloud.oracle.com** (choose a home region close to New York, e.g. US East Ashburn).
2. **Create a VM instance**: image **Ubuntu 22.04 or 24.04**, shape **VM.Standard.E2.1.Micro** or **Ampere A1** (both "Always Free eligible"). Download the SSH key it offers.
3. Open the dashboard port: Instance → Subnet → Security list → **Add ingress rule**: source `0.0.0.0/0`, TCP port `8765`.
4. Connect: `ssh -i your-key.key ubuntu@YOUR_SERVER_IP`
5. On the server, run:
   ```bash
   sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
   sudo usermod -aG docker $USER && newgrp docker
   sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT && sudo netfilter-persistent save   # Oracle's Ubuntu firewall
   mkdir -p ~/momentum-desk && cd ~/momentum-desk
   ```
6. Copy the app from your PC (in a Windows terminal, in the app folder):
   ```bash
   scp -i your-key.key -r config.py notifier.py momentum_scanner.py web requirements.txt Dockerfile docker-compose.yml .env ubuntu@YOUR_SERVER_IP:~/momentum-desk/
   ```
7. On the server, edit `.env` (`nano .env`) and set **`DASHBOARD_PASSWORD=`** (required online) and `PUBLIC_URL=http://YOUR_SERVER_IP:8765`.
8. Start it:
   ```bash
   docker compose up -d --build
   docker compose logs -f          # watch it start; Ctrl+C to leave (it keeps running)
   ```
   Your phone gets "Momentum Desk is online". Open `http://YOUR_SERVER_IP:8765` anywhere and log in with the password (any user name).

It restarts automatically after crashes and reboots. To update later, copy the new files again, then run `docker compose up -d --build`.
No Docker? Use `deploy/momentum-desk.service` (instructions inside the file).

### Option B: GitHub Actions (no server, no card)
GitHub starts the scanner every weekday at 07:20/08:20 New York time (depending on daylight saving). It runs until 11:05, sends all the alerts, then stops.
1. Create a free GitHub account. Make a **public** repository (public repos get unlimited free minutes; your keys stay secret, see step 3) and upload: `config.py`, `notifier.py`, `momentum_scanner.py`, `web/`, `requirements.txt`, `.github/workflows/scanner.yml`. **Never upload `.env`.**
2. *(A private repo also works, but the free 2,000 minutes a month run out after about 10 trading days.)*
3. Repository → Settings → Secrets and variables → Actions → **New repository secret**. Add `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` and your alert settings (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, or `NTFY_TOPIC`). Optionally add `ACCOUNT_SIZE` as a secret too (for share counts; a secret keeps it out of the public logs), and `RISK_PCT` under the **Variables** tab.
4. Actions tab → "Momentum Desk morning scan" → **Run workflow** to test. You should get "Momentum Desk is online".
Notes: GitHub sometimes starts scheduled jobs 5 to 15 minutes late, which is fine since the trading window starts later. A backup start at 13:05 UTC covers a dropped job. Trade results and today's alert state are kept between runs in the Actions cache, so the scanner learns which setups win. To check Telegram from GitHub: Actions → Momentum Desk morning scan → Run workflow → tick "Only send a test Telegram message".

### Option C: Your own PC
Double-click **INSTALL_AUTOSTART.bat**. The app runs silently in the background from then on and starts again every time you log in. Logs go to `desk.log`.
Set Windows **Sleep → Never** (while plugged in), or alerts stop while the PC sleeps. Remove it with `UNINSTALL_AUTOSTART.bat`.

---

## 4. What changed in this version

**More accurate signals**
- **No more false "spread over 2%" skips.** The free IEX feed only shows IEX's own quotes, which are often far wider than the real market. Spread is now a hint ("check your broker"), not a deal-breaker, unless you use the paid SIP feed.
- **Halt detection.** No trades for 5+ minutes during market hours → blocked and flagged "possibly halted".
- **Max price enforced on the server.** A breakout already above the max price shows TOO LATE instead of BUY NOW, so the dashboard and phone alerts always agree.
- **Resistance check.** If a key level (high of day, yesterday's high, 52-week high...) sits less than 0.75R above the entry, BUY NOW becomes BUY SMALL, with a tip telling you where to take profits.
- A pattern without a usable stop never shows GET READY.
- Earnings only show when they're within 7 days (it used to always show).

**Clearer**
- Tips are short, with a bold title and one line of explanation, grouped as **Deal-breakers / In your favor / Be careful**, plus a tally at the top of each stock.
- **Your risk** box: enter your account size and risk %, and every plan tells you the exact number of shares and the dollars at risk.
- **Recent alerts** panel and a **Phone alerts** status badge.
- The "Learn" section is folded away until you need it.

**Production-ready**
- Keys live in `.env`, not in the code. Optional dashboard password, `/healthz` health check, clean shutdown, logs with timestamps.
- Slower scanning overnight and on weekends (saves API calls). Docker, systemd and GitHub Actions files are included.
- `signals_log.csv` now also records grade and max price. The old file is kept as `signals_log_old_<date>.csv`.

## Limits (honest)
- The free IEX feed sees about 2–3% of US volume. Volume ratios are consistent, but quotes and spreads are a sample.
- Yahoo and Finviz may block cloud servers' IP addresses. If they do, float and short interest show "unknown" (half credit in the score) and everything else keeps working.
- Rules can't see Level 2 or the tape. Always confirm in your broker.
