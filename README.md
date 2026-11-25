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

**Alerts are two lines you can act on from the lock screen:**
```
🟢 BUY APUS $2.07 · max $2.10
Stop $1.94 (-6.3%) · ½ $2.20 · Tgt $2.33 (2.0R)
A 86 · Micro pullback · RVOL 126x · Float 0.69M · +30%
📊 Backtest Micro pullback · A: 38 trades · 61% win · +0.52R avg · Live 12W/6L +3.4R
⚠ Micro-cap ($20M) · Late morning
⏱ broke 09:58:13 · sent 09:58:18 (+5s) · Schwab live tape
```
**Core pillars are hard gates** (`MUST_PILLARS=price,float,rvol,catalyst`): a big float, a stock outside $2-20, RVOL under 5x or no real catalyst (analyst notes and vague PRs don't count) is NO TRADE, never GET READY or BUY. An unknown float (data blocked) doesn't block. Set `MUST_PILLARS=` to go back to weighted scoring only.

---

## 2b. Real-time Level 2 (order book), free with Schwab

Alpaca only gives the best bid and ask (Level 1). For the **real order book** (every price level, share sizes, market makers), connect the free **Charles Schwab Trader API**. It needs a Schwab brokerage account (it can be empty).

1. Go to **developer.schwab.com**, sign up and **Create App**: API product **Market Data Production**, callback URL **`https://127.0.0.1`**. Wait until the app status is **Ready For Use** (it can take a few days).
2. Copy the app's **App Key** and **Secret** into `.env`: `SCHWAB_APP_KEY=...` and `SCHWAB_APP_SECRET=...`
3. Log in once: `python momentum_scanner.py --schwab-login`. Sign in with your **brokerage** login, click Allow. The browser ends on an error page at `https://127.0.0.1/?code=...`: **that's normal**. Copy the whole address and paste it into the terminal within about 30 seconds.
   On a server, either run it there (`docker compose exec -it momentum-desk python momentum_scanner.py --schwab-login`) or use the **Connect Schwab** box on the dashboard.
4. **Every 7 days** Schwab asks you to log in again (their rule). The app sends a phone alert a day before it expires.

What you get on the dashboard, for the selected stock (updated every second):
- **The ladder**: the top 10 bids and asks with shares and who posted them. Big orders (walls) are in bold, and the entry and stop prices are tagged.
- **What the book is telling you**, in plain English: buyers or sellers stacked, big buyers and sellers (walls), thin price gaps, **hidden sellers/buyers** (an order that keeps refilling), walls that got **eaten** (a real breakout or breakdown) or **pulled** (likely fake), the real spread, and how many shares must trade to reach your entry.
- **Every possibility from here**: what it means if the ask gets lifted, if the big seller gets eaten or keeps refilling or vanishes, if the bid wall holds or breaks, if nothing changes.
- A new weighted checklist item, **"Order book with you (Level 2)"** (2 points). It fails if sellers are stacked, a hidden seller sits at the price, a big seller sits between the entry and +1R, or the spread is over 2%. When Level 2 isn't connected the item doesn't exist, so nothing changes for you.
- The **real** bid and ask replace the IEX sample for the spread check, and BUY alerts get a "Level 2:" line.

Also new for every stock: **"Why <signal>, and what would change it"**. It lists every reason the signal is what it is, and every way it can change (to BUY NOW, EXIT NOW, TOO LATE, NO TRADE...) with exact prices and volumes.

`START_DEMO.bat` shows a fake order book, so you can see the panel when the market is closed.

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
   mkdir -p ~/momentum-desk/data && sudo chown 1000:1000 ~/momentum-desk/data && cd ~/momentum-desk   # the app runs as user 1000 and saves here
   ```
6. Copy the app from your PC (in a Windows terminal, in the app folder):
   ```bash
   scp -i your-key.key -r config.py notifier.py level2.py yahoo_feed.py pillars.py momentum_scanner.py replay.py web requirements.txt Dockerfile docker-compose.yml .env ubuntu@YOUR_SERVER_IP:~/momentum-desk/
   ```
7. On the server, edit `.env` (`nano .env`) and set **`DASHBOARD_PASSWORD=`** (required online) and `PUBLIC_URL=http://YOUR_SERVER_IP:8765`.
8. Start it:
   ```bash
   docker compose up -d --build
   docker compose logs -f          # watch it start; Ctrl+C to leave (it keeps running)
   ```
   Your phone gets "Momentum Desk is online". Open `http://YOUR_SERVER_IP:8765` anywhere and log in with the password (any user name).

9. Level 2: open the dashboard and use the **Connect Schwab** box (or copy `schwab_tokens.json` from your PC into `~/momentum-desk/data/`).
10. **Stop the copy on your PC** (`UNINSTALL_AUTOSTART.bat`), or you get every alert twice and the two Level 2 streams can kick each other off.

It restarts automatically after crashes and reboots. To update later, copy the new files again, then run `docker compose up -d --build`.
No Docker? Use `deploy/momentum-desk.service` (instructions inside the file).

### Option B: GitHub Actions (no server, no card)
GitHub starts the scanner every weekday at 07:20/08:20 New York time (depending on daylight saving). It runs until 11:05, sends all the alerts, then stops.
1. Create a free GitHub account. Make a **public** repository (public repos get unlimited free minutes; your keys stay secret, see step 3) and upload: `config.py`, `notifier.py`, `level2.py`, `yahoo_feed.py`, `pillars.py`, `momentum_scanner.py`, `web/`, `requirements.txt`, `.github/workflows/scanner.yml`. **Never upload `.env`.**
2. *(A private repo also works, but the free 2,000 minutes a month run out after about 10 trading days.)*
3. Repository → Settings → Secrets and variables → Actions → **New repository secret**. Add `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` and your alert settings (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, or `NTFY_TOPIC`). Optionally add `ACCOUNT_SIZE` as a secret too (for share counts; a secret keeps it out of the public logs), and `RISK_PCT` under the **Variables** tab.
4. Actions tab → "Momentum Desk morning scan" → **Run workflow** to test. You should get "Momentum Desk is online".
Notes: GitHub sometimes starts scheduled jobs 5 to 15 minutes late, which is fine since the trading window starts later. A backup start at 13:05 UTC covers a dropped job. Trade results and today's alert state are kept between runs in the Actions cache, so the scanner learns which setups win. To check Telegram from GitHub: Actions → Momentum Desk morning scan → Run workflow → tick "Only send a test Telegram message".

### Option C: Your own PC
Double-click **INSTALL_AUTOSTART.bat**. The app runs silently in the background from then on and starts again every time you log in. Logs go to `desk.log`.
Set Windows **Sleep → Never** (while plugged in), or alerts stop while the PC sleeps. Remove it with `UNINSTALL_AUTOSTART.bat`.

---

## Full-market candles (Yahoo) and the Replay tool

**Candles and volume now come from Yahoo Finance (full market, free).** The free Alpaca feed is IEX only, about 2–3% of trading: on 2026-11-23 it saw 28,331 BNAI shares in the first 90 minutes, while the real number was 15.1 million. Volume checks, VWAP and pullback patterns now use the full tape. Alpaca IEX stays on as the automatic backup, per stock. The dashboard pill shows which one is live ("Candles · full volume" or "IEX sample only"). If Yahoo blocks the server during the window, you get one Telegram warning. To turn it off: `YAHOO_CANDLES=0`.

**Backtest, and let the results drive the alerts:**
```bash
python replay.py --days 40 --save-stats      # last 40 trading days, each day's top gainers found automatically (~12 min)
```
It prints win rate and average R per setup and per setup + pillar grade, and saves `setup_stats.json`. The live bot reloads it on its own (no restart):
- every alert shows the matching record (`📊 Backtest Micro pullback · A: 38 trades · 61% win · +0.52R avg`)
- a setup or setup + grade that **lost money over 12+ trades is muted** (NO TRADE "muted: … lost in the backtest"). `MUTE_LOSING_SETUPS=0` shows the record without muting; `STATS_MIN_TRADES` sets the sample size.
Re-run it weekly (or after any rule change) so the numbers stay current.

**Fast triggers with Schwab connected:** the Schwab stream's real-time trade prints now (1) replace the IEX/Yahoo price in every scan and (2) wake the scanner the instant a print crosses an armed trigger, a stop, +1R or the target, instead of waiting for the next 3-second scan. The 5-second breakout confirmation is timed exactly too. Each BUY alert ends with `⏱ broke 09:58:13 · sent 09:58:18 (+5s) · Schwab live tape` (or `polled` without Schwab).

**Replay any day through the real rules** (uses Alpaca's free full-market history, which is free once it's 15+ minutes old; it never touches your live bot's files):
```bash
python replay.py                                        # today's top gainers, after the window
python replay.py --date 2026-11-20 --symbols EVH,GAP    # a past day (name the stocks)
python replay.py --feed iex                             # what the old IEX-only bot would have seen
python replay.py --verbose --csv day.csv                # every signal change, saved to a file
```
It prints the trades the bot would have taken (entry, stop, exit, result in R) and, for every stock, the rules that blocked it most. Use it before changing any rule.

## The 5 pillars, graded (pillars.py)

Each pillar gets a score from 0 to 100 built from several indicators. The scores combine into one **Pillar Score** with a grade (A+ / A / B / C / D). The grade is shown in the stock list, in the stock view (open it to see every indicator and its source), in BUY alerts and in the replay report. Stocks with the same signal are ranked by Pillar Score.

| Pillar (weight) | Indicators | Pass |
| --- | --- | --- |
| **Good catalyst** (25) | headline type graded (FDA approval / buyout / positive Phase 3 = 100 … contract, earnings beat = 85 … partnership = 75 … plain earnings report = 65 … vague PR = 30), freshness, second source, same-day SEC 8-K, red flags (offering, reverse split, delisting notice, FDA rejection, lawsuit, miss). Letters of intent are capped at 40. | specific type and score 70+ |
| **High relative volume** (25) | day RV vs the same minute on the last 10 days, **RV of the last 15 minutes** (is the crowd still here?), 5-minute volume trend | 5x+ |
| **Low float** (20) | float (5M / 10M / 20M tiers), float rotation (volume / float), short interest, insider and institution ownership | under 20M |
| **High volume** (15) | shares traded today (full market), dollar volume, % of a normal full day, buy vs sell volume over 10 minutes. Pre-market bars are 1/5 of the regular-session bars. | 1M+ shares |
| **Price $2-20** (15) | price zone ($4-12 is the sweet spot), 14-day average daily range (ATR%), gap, tick size under $2 | $2-20 |

New decision check (minor, 1 point): **Crowd still here**. The last 15 minutes must trade at 2x+ their normal volume. A big relative volume from a 7am spike that has since died out no longer counts.

Sources: Ross Cameron / Warrior Trading stock-selection criteria; Andrew Aziz, *How to Day Trade for a Living* (stocks in play); Chan (2003, *J. Financial Economics*): moves with news keep drifting, moves without news reverse; Gervais, Kaniel & Mingelgrin (2001, *J. Finance*): high-volume return premium; Lee & Swaminathan (2000, *J. Finance*): momentum and volume; Tetlock (2007) and Tetlock et al. (2008, *J. Finance*): headline words predict the reaction.

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
- Candles come from Yahoo (full volume), but quotes and spreads are still the IEX sample. If Yahoo is blocked, the bot falls back to IEX (about 2–3% of US volume) and signals get rarer.
- Yahoo and Finviz may block cloud servers' IP addresses. If they do, float and short interest show "unknown" (half credit in the score) and everything else keeps working.
- Without Schwab connected, the rules can't see Level 2. With it, Level 2 shows orders, not trades: orders can be pulled in a split second and big players hide size. Always confirm in your broker.
