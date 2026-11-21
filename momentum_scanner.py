#!/usr/bin/env python3
"""
MOMENTUM DESK - a momentum stock scanner (Ross Cameron style) with phone alerts.
It finds the best small-cap movers, explains each one in plain English and tells you exactly what to do.
It never places orders: you decide and trade in your own broker. Education only, not financial advice.

DATA SOURCES (all free)
  Alpaca        live prices, 1-minute candles, daily candles, Benzinga news
  Yahoo Finance float, short interest, ownership, company profile, 52-week range, full-market volume
  Finviz        float / short interest backup, extra headlines
  SEC EDGAR     official filings: offerings (424B, S-1, S-3), 8-K events, late filings

START
  Windows: double-click START.bat (START_DEMO.bat = fake data).
  Or:      python momentum_scanner.py [--demo] [--no-browser]
  Tools:   python momentum_scanner.py --test-alert       send a test notification to your phone
           python momentum_scanner.py --telegram-setup   find your Telegram chat id
  Settings live in .env (copy .env.example). Running 24/7 in the cloud: see README.md.

HONEST LIMITS
  * The free IEX feed sees only part of the market (volume, spread and quotes are a sample).
    Relative volume compares IEX with its own 10-day history (consistent); full-market volume comes from Yahoo.
  * Yahoo and Finviz are public web pages, not official APIs: if they block or change, those fields show
    "unknown" and the rest keeps working.
  * Real Level 2 needs a free Schwab developer app + a Schwab brokerage account (see level2.py). Without it: no order book.
  * No halt feed (halts are inferred from missing trades).
"""
import os, re, sys, csv, json, math, time, random, socket, signal, logging, threading, webbrowser, hmac, base64
import urllib.request, urllib.parse, urllib.error, http.cookiejar
from datetime import datetime, timedelta, date, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import config as C
from notifier import Notifier, AlertManager
from level2 import Level2, login_cli as schwab_login

log = logging.getLogger("desk")
NY = C.NY
KEY, SEC, FEED, WATCH = C.KEY, C.SEC, C.FEED, C.WATCH
WEND = C.TRADE_UNTIL
DEMO, PORT, INTERVAL, LOG_FILE = C.DEMO, C.PORT, C.INTERVAL, C.LOG_FILE
DATA = "https://data.alpaca.markets"
PAPER = "https://paper-api.alpaca.markets/v2"   # used only for the market clock/calendar/asset names; no orders are ever sent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEC_UA = "MomentumDesk/1.0 (personal research tool)"
L2 = Level2(demo=DEMO)      # real-time order book (Schwab). Off until SCHWAB_APP_KEY is set

# ---------------------------------------------------------------- helpers
def http_get(url, params=None, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s from %s: %s" % (e.code, url.split("?")[0], e.read()[:200].decode("utf8", "ignore")))

def http_text(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        return r.read().decode("utf8", "ignore")

def alp(path, **params):
    return http_get(DATA + path, params, {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC})

def paper(method, path):
    req = urllib.request.Request(PAPER + path, method=method, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError("Alpaca said: " + e.read()[:250].decode("utf8", "ignore"))

def parse_t(t):
    """ISO time from Alpaca. Handles 'Z' and nanosecond fractions, which older Pythons reject."""
    t = re.sub(r"(\.\d{6})\d+", r"\1", t.replace("Z", "+00:00"))
    return datetime.fromisoformat(t)

def today_start():
    return datetime.now(NY).replace(hour=4, minute=0, second=0, microsecond=0)

def strip_tags(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"').strip()

def parse_num(t):
    t = (t or "").strip().replace(",", "")
    m = re.match(r"^(-?[\d.]+)\s*([KMBT%]?)$", t)
    if not m:
        return None
    return float(m.group(1)) * {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(m.group(2), 1)

# typical share of a day's volume already traded by a given minute (ET); fallback when history is missing
CURVE = [(240, 0.0), (480, .03), (570, .07), (600, .20), (630, .28), (660, .34), (720, .43), (840, .55), (960, 1.0)]
def expected_frac(mins):
    if mins <= CURVE[0][0]:
        return .005
    for (a, fa), (b, fb) in zip(CURVE, CURVE[1:]):
        if mins <= b:
            return max(.005, fa + (fb - fa) * (mins - a) / (b - a))
    return 1.0

def ema(vals, n):
    k, e, out = 2 / (n + 1), None, []
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out

def macd_positive(closes):
    if len(closes) < 35:
        return None
    line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    sig = ema(line, 9)
    return line[-1] > 0 and line[-1] > sig[-1]

def vwap_series(bars):
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        pv += b["vw"] * b["v"]; vv += b["v"]
        out.append(pv / vv if vv else None)
    return out

# ---------------------------------------------------------------- extra data sources: Yahoo, Finviz, SEC
class Yahoo:
    """Yahoo Finance needs a cookie + 'crumb' token; this handles it and refreshes it when it expires."""
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.crumb, self.lock = None, threading.Lock()
    def _get(self, url):
        with self.op.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            return r.read()
    def call(self, path, params):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if not self.crumb:
                        try:
                            self._get("https://fc.yahoo.com")
                        except Exception:
                            pass      # it answers 404 but sets the cookie we need
                        self.crumb = self._get("https://query1.finance.yahoo.com/v1/test/getcrumb").decode().strip()
                    return json.loads(self._get("https://query1.finance.yahoo.com" + path + "?" + urllib.parse.urlencode(dict(params, crumb=self.crumb))))
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403) and attempt == 0:
                        self.crumb = None
                        continue
                    raise
YH = Yahoo()

def rv(d, k):
    v = (d or {}).get(k)
    return v.get("raw") if isinstance(v, dict) else v

def yahoo_profile(s):
    j = YH.call("/v10/finance/quoteSummary/" + urllib.parse.quote(s),
                {"modules": "defaultKeyStatistics,assetProfile,summaryDetail,calendarEvents,price"})
    r = ((j.get("quoteSummary") or {}).get("result") or [{}])[0]
    ks, ap, sd, pr, ce = [r.get(k) or {} for k in ("defaultKeyStatistics", "assetProfile", "summaryDetail", "price", "calendarEvents")]
    ed = (ce.get("earnings") or {}).get("earningsDate") or []
    pct = lambda v: v * 100 if isinstance(v, (int, float)) else None
    return dict(name=pr.get("longName") or pr.get("shortName"), float=rv(ks, "floatShares"), shares=rv(ks, "sharesOutstanding"),
                short_pct=pct(rv(ks, "shortPercentOfFloat")), short_ratio=rv(ks, "shortRatio"),
                inst_pct=pct(rv(ks, "heldPercentInstitutions")), insider_pct=pct(rv(ks, "heldPercentInsiders")),
                country=ap.get("country"), sector=ap.get("sector"), industry=ap.get("industry"), employees=ap.get("fullTimeEmployees"),
                summary=(ap.get("longBusinessSummary") or "")[:700], website=ap.get("website"),
                mcap=rv(pr, "marketCap") or rv(sd, "marketCap"), hi52=rv(sd, "fiftyTwoWeekHigh"), lo52=rv(sd, "fiftyTwoWeekLow"),
                avg10=rv(sd, "averageVolume10days"), earnings=ed[0].get("fmt") if ed and isinstance(ed[0], dict) else None)

def yahoo_quotes(syms):
    j = YH.call("/v7/finance/quote", {"symbols": ",".join(syms)})
    out = {}
    for q in (j.get("quoteResponse") or {}).get("result") or []:
        t = q.get("regularMarketTime")
        out[q["symbol"]] = dict(vol=q.get("regularMarketVolume"), avg10=q.get("averageDailyVolume10Day"),
                                day=datetime.fromtimestamp(t, NY).date().isoformat() if t else None)
    return out

FV_FIELDS = {"Shs Float": "float", "Shs Outstand": "shares", "Short Float": "short_pct", "Short Ratio": "short_ratio",
             "Inst Own": "inst_pct", "Insider Own": "insider_pct", "Market Cap": "mcap", "Earnings": "earnings"}
def finviz(s):
    h = http_text("https://finviz.com/quote.ashx?t=" + urllib.parse.quote(s), {"User-Agent": UA})
    out = {}
    for lab, k in FV_FIELDS.items():
        m = re.search(re.escape(lab) + r"(?:</[^>]+>\s*)+<td[^>]*>(.{0,300}?)</td>", h, re.S)
        if m:
            txt = strip_tags(m.group(1))
            out[k] = txt if k == "earnings" else parse_num(txt.split()[0] if txt else "")
    news, last = [], None
    i = h.find('id="news-table"')
    if i >= 0:
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", h[i:i + 60000], re.S):
            a = re.search(r'<a[^>]*href="([^"]+)"[^>]*class="tab-link-news"[^>]*>(.*?)</a>', row, re.S) or \
                re.search(r'<a[^>]*class="tab-link-news"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', row, re.S)
            if not a:
                continue
            td = re.search(r"<td[^>]*>(.*?)</td>", row, re.S)
            dt = strip_tags(td.group(1)) if td else ""
            m = re.search(r"(Today|[A-Z][a-z]{2}-\d{2}-\d{2})?\s*(\d{1,2}:\d{2}[AP]M)", dt)
            when = None
            if m:
                if m.group(1) == "Today":
                    last = datetime.now(NY).date()
                elif m.group(1):
                    last = datetime.strptime(m.group(1), "%b-%d-%y").date()
                if last:
                    when = datetime.combine(last, datetime.strptime(m.group(2), "%I:%M%p").time(), NY)
            url = a.group(1)
            news.append(dict(h=strip_tags(a.group(2)), url=url if url.startswith("http") else "https://finviz.com" + url,
                             t=when.isoformat() if when else "", src="Finviz"))
            if len(news) >= 8:
                break
    out["news"] = news
    return out

SEC_MAP = {"m": None}
def sec_filings(s):
    if SEC_MAP["m"] is None:
        j = json.loads(http_text("https://www.sec.gov/files/company_tickers.json", {"User-Agent": SEC_UA}))
        SEC_MAP["m"] = {v["ticker"].upper(): int(v["cik_str"]) for v in j.values()}
    cik = SEC_MAP["m"].get(s.upper())
    if not cik:
        return []
    j = json.loads(http_text("https://data.sec.gov/submissions/CIK%010d.json" % cik, {"User-Agent": SEC_UA}))
    rf, today, out = j["filings"]["recent"], datetime.now(NY).date(), []
    for form, fd, acc, doc, desc in zip(rf["form"], rf["filingDate"], rf["accessionNumber"], rf["primaryDocument"], rf["primaryDocDescription"]):
        age = (today - date.fromisoformat(fd)).days
        if age > 120:
            break
        out.append(dict(form=form, date=fd, age=age, desc=desc,
                        url="https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (cik, acc.replace("-", ""), doc)))
        if len(out) >= 25:
            break
    return out

ENRICH, WANT, YQ = {}, set(), {}
def enrich(s):
    e, src = dict(t=time.time()), []
    for name, fn, key in (("Yahoo", yahoo_profile, "y"), ("Finviz", finviz, "fv"), ("SEC", sec_filings, "sec")):
        try:
            e[key] = fn(s); src.append(name)
        except Exception as x:
            e[key + "_err"] = str(x)[:120]
    e["src"] = src
    if len(src) < 2:
        e["t"] = time.time() - 4 * 3600 + 600      # partly failed: retry in 10 minutes
    return e

def enrich_loop():
    lastq = 0
    while True:
        try:
            want = list(WANT)
            todo = [s for s in want if s not in ENRICH or time.time() - ENRICH[s]["t"] > 4 * 3600]
            for s in todo[:3]:
                ENRICH[s] = enrich(s)
            if want and time.time() - lastq > 45:
                lastq = time.time()
                try:
                    YQ.update(yahoo_quotes(want))
                except Exception:
                    pass
        except Exception:
            log.exception("loop error")
        time.sleep(1.5)

def fundamentals(s):
    e = ENRICH.get(s) or {}
    y, f = e.get("y") or {}, e.get("fv") or {}
    out = dict(loaded=bool(e), sources=e.get("src", []), sec=e.get("sec"), fv_news=f.get("news", []))
    for k in ("float", "shares", "short_pct", "short_ratio", "inst_pct", "insider_pct", "mcap", "earnings"):
        out[k], out[k + "_src"] = None, None
        for name, d in (("Yahoo", y), ("Finviz", f)):
            if d.get(k) not in (None, 0, "", "-"):
                out[k], out[k + "_src"] = d[k], name
                break
    out["name"] = y.get("name") or ASSETS.get(s) or None
    for k in ("country", "sector", "industry", "employees", "summary", "website", "hi52", "lo52", "avg10"):
        out[k] = y.get(k)
    return out

# ---------------------------------------------------------------- market calendar (real holidays and early closes)
CAL = {"t": 0, "days": None}
def calendar():
    if DEMO:
        return None
    if time.time() - CAL["t"] > 3600 or CAL["days"] is None:
        try:
            d0 = datetime.now(NY).date()
            r = paper("GET", "/calendar?start=%s&end=%s" % (d0.isoformat(), (d0 + timedelta(days=20)).isoformat()))
            days = {}
            for x in r:
                dd = datetime.strptime(x["date"], "%Y-%m-%d").date()
                o, c = [dtime(*map(int, x[k].split(":"))) for k in ("open", "close")]
                days[dd] = (datetime.combine(dd, o, NY), datetime.combine(dd, c, NY))
            CAL.update(t=time.time(), days=days)
        except Exception:
            CAL["t"] = time.time() - 3300
    return CAL["days"]

def day_window(d, days):
    """Today's schedule: dict(open, close, win_start, win_end) in New York time, or None if the market is closed."""
    if days is not None and d in days:
        o, c = days[d]
    elif days is not None and days and d <= max(days):
        return None
    elif d.weekday() >= 5:
        return None
    else:
        o, c = datetime.combine(d, dtime(9, 30), NY), datetime.combine(d, dtime(16), NY)
    ws = max(datetime.combine(d, C.TRADE_FROM, NY), datetime.combine(d, dtime(4), NY))
    we = min(c, datetime.combine(d, WEND, NY))
    return dict(open=o, close=c, win_start=ws, win_end=max(we, ws))

def market_state(now):
    days = calendar()
    tw = day_window(now.date(), days)
    if DEMO:
        wopen, session = True, "Demo"
        tw = dict(open=now - timedelta(minutes=30), close=now + timedelta(hours=6), win_start=now - timedelta(minutes=5), win_end=now + timedelta(hours=1))
    elif not tw:
        wopen, session = False, "Market closed today"
    else:
        wopen = tw["win_start"] <= now < tw["win_end"]
        if now < datetime.combine(now.date(), dtime(4), NY):
            session = "Closed (pre-market starts 04:00 New York)"
        elif now < tw["open"]:
            session = "Pre-market"
        elif now < tw["close"]:
            session = "Market open"
        elif now < datetime.combine(now.date(), dtime(20), NY):
            session = "After-hours"
        else:
            session = "Closed"
    nxt = tw if tw and now < tw["win_end"] else None
    for i in range(1, 25):
        if nxt:
            break
        w = day_window(now.date() + timedelta(days=i), days)
        if w:
            nxt = w
    if not nxt:
        nxt = dict(win_start=now + timedelta(hours=12), win_end=now + timedelta(hours=13))
    return dict(window_open=wopen, session=session, today=tw,
                win_start=int(nxt["win_start"].timestamp() * 1000), win_end=int(nxt["win_end"].timestamp() * 1000))

# ---------------------------------------------------------------- setup detection
def detect(bars, p, now):
    """Return an ARMED plan if price is just under a trigger, else None."""
    if len(bars) < 10:
        return None
    look = bars[-25:]
    hi = max(range(len(look)), key=lambda i: look[i]["h"])
    sw = look[hi]["h"]
    after = look[hi + 1:]
    base = min(b["l"] for b in look[:hi + 1])
    run = sw - base
    if run >= 0.03 * sw and 1 <= len(after) <= 12:
        pl = min(b["l"] for b in after)
        depth = (sw - pl) / run
        if depth <= (0.55 if len(after) <= 6 else 0.35) and pl <= p <= sw and (sw - p) / sw <= 0.03:
            runv = sum(b["v"] for b in look[max(0, hi - 2):hi + 1]) / min(3, hi + 1)
            pullv = sum(b["v"] for b in after) / len(after)
            return dict(setup="Micro pullback" if len(after) <= 3 else "Bull flag" if len(after) <= 6 else "Flat-top breakout", state="ARMED",
                        trigger=sw, stop=pl, lightvol=pullv < runv, pullv=pullv,
                        note="It ran up %.0f%%, then paused for %d minute%s and gave back %d%% of the run on %s volume." % (
                            run / base * 100, len(after), "" if len(after) == 1 else "s", depth * 100, "light (good)" if pullv < runv else "heavy (less good)"))
    if now.time() >= dtime(9, 30):
        pm = [b["h"] for b in bars if parse_t(b["t"]).astimezone(NY).time() < dtime(9, 30)]
        if pm:
            pmh = max(pm)
            if pmh * 0.985 <= p <= pmh:
                return dict(setup="Pre-market high break", state="ARMED", trigger=pmh,
                            stop=min(b["l"] for b in bars[-6:]), note="The highest price before the open was $%.2f. A break above it often brings in buyers." % pmh)
    return None

# ---------------------------------------------------------------- analysis
STATE = {}
ORDER = {"EXIT NOW": 0, "SELL HALF": 0, "BUY NOW": 1, "BUY HALF": 2, "HOLD": 3, "GET READY": 4, "WAIT": 5, "TOO LATE": 6,
         "CLOSED": 7, "NO TRADE": 8}
STRONG = ("fda", "approval", "approved", "phase", "trial", "merger", "acquisition", "acquire", "contract", "partnership", "agreement",
          "earnings", "revenue", "guidance", "patent", "clearance", "designation", "award", "launch", "breakthrough", "upgrade", "buyout", "collaboration")
DILUTION = ("offering", "registered direct", "private placement", "reverse split", "shelf", "dilution", "warrants", "at-the-market", "atm program", "convertible")
RISKY_COUNTRIES = ("China", "Hong Kong", "Macau", "Malaysia", "Singapore")

def classify(news):
    if not news:
        return "none"
    heads = " ".join(n["h"].lower() for n in news[:3])
    if any(w in heads for w in DILUTION):
        return "dilution"
    return "strong" if any(w in heads for w in STRONG) else "weak"

def merged_news(inp, now):
    """Alpaca (Benzinga) news + Finviz headlines, newest first, without duplicates. 'recent' = last 18 hours."""
    allv, seen = [], set()
    for n in list(inp["news"]) + list((inp.get("fund") or {}).get("fv_news") or []):
        k = re.sub(r"[^a-z0-9]", "", n["h"].lower())[:50]
        if not k or k in seen:
            continue
        seen.add(k)
        allv.append(dict(n, src=n.get("src", "Benzinga")))
    def ts(n):
        try:
            return parse_t(n["t"]).timestamp()
        except Exception:
            return 0
    allv.sort(key=ts, reverse=True)
    recent = [n for n in allv if ts(n) and now.timestamp() - ts(n) <= 18 * 3600]
    return allv[:10], recent

# ---------------------------------------------------------------- explain every signal: what it means, why, what would change it
MEANS = {
    "BUY NOW": "The only time to buy: it broke out the right way and passed every MUST check.",
    "BUY HALF": "A buy, at half size: it qualifies but with less margin than a full BUY.",
    "HOLD": "You're in. The plan is frozen: same stop, same targets. Do nothing unless a rule below fires.",
    "SELL HALF": "+1R reached: bank half, move the stop to your entry. The trade can no longer lose.",
    "EXIT NOW": "Sell everything now. A rule closed the trade: a small loss or a locked gain, as planned.",
    "CLOSED": "This trade is finished. The stock is only tradable again with a brand-new setup.",
    "GET READY": "Heads-up only, NOT a buy. A setup is loaded; the buy comes only if it breaks out the right way.",
    "WAIT": "No safe entry yet. Something specific is missing (listed below).",
    "TOO LATE": "It already ran past the entry. Buying now means a far stop and little reward: don't chase.",
    "NO TRADE": "Don't buy. A hard rule is broken (listed below).",
}

def explain_signal(x):
    """Every reason the signal is what it is, and every way it can change (with prices). Returns dict(means, why, flip)."""
    u = lambda v: "$%.2f" % v if v is not None else "-"
    sig, plan, tr, p, vw = x["sig"], x["plan"], x["tr"], x["p"], x["vw"]
    entry, stop, target, mx = x["entry"], x["stop"], x["target"], x["max_price"]
    t1 = round(entry + (entry - stop), 2) if entry and stop else None
    pace = int(1.5 * plan["pullv"]) if plan and plan.get("pullv") else None
    ra, rr = x["res_above"], x["room_r"]
    detail = {c[0]: c[3] for c in x["checks"]}
    NEED = {
        "Volume 3x+ normal": "relative volume of 3x normal or more",
        "Above VWAP": ("price back above VWAP %s (now %s)" % (u(vw), u(p))) if vw else "not enough candles for VWAP yet",
        "No offering / dilution": "no dilution: can't change today, the company is selling shares",
        "Clean pullback pattern": ("the %s still there on the next scan (one-scan flickers are ignored)" % plan["setup"].lower()) if plan else
            "a run-up of 3%+, then 1-12 calm candles that keep most of the gain, with price back within 3% of the high (or price testing the pre-market high)",
        "Tight stop (8% or less)": ("a tighter pullback: the stop is %.1f%% away, 8%% or less is allowed" % x["risk_pct"]) if plan and x["risk_pct"] else
            "a pattern first (the stop is the pullback low)",
        "Room to run (1R+ to resistance)": ("a break above %s %s first (only %.1fR above the entry)" % (ra["label"].lower(), u(ra["price"]), rr))
            if ra and rr is not None else "clear",
        "Liquid ($1M+ traded today)": ("$1M+ traded today (now $%.2fM)" % (x["dollars"] / 1e6)) if x["dollars"] else "full-market volume (not loaded yet)",
        "Breakout on a volume surge": ("the breakout minute needs %s+ shares (1.5x the pullback's pace)" % "{:,}".format(pace)) if pace else
            "the breakout needs 1.5x the pullback volume",
        "Breakout holding (no rejection wick)": "price must hold in the top half of the breakout candle (no slam from the high)",
        "Real news catalyst": "a strong headline (FDA, contract, earnings, merger...)",
        "Low float (under 20M)": "a float under 20M shares (fixed: can't change)",
        "Buyers in control (last 10 min)": "more volume on green candles than red over the last 10 minutes (1.2x+)",
        "5-minute trend up": "the 5-minute chart closing above its 9 EMA",
        "Order book with you (Level 2)": "the Level 2 book not leaning against the trade (no stacked sellers, hidden seller or big seller before +1R)",
        "This setup has an edge (past results)": "this setup type to stop losing in your trade history",
        "Up 10%+ today": "the stock up 10%+ on the day",
        "Trend up (9 EMA + MACD)": "price above the 9 EMA with MACD positive",
        "Quiet pullback volume": "lighter volume on the pullback than on the run",
        "Not stretched (<20% over VWAP)": "price within 20%% of VWAP %s" % u(vw),
        "Market not weak": "SPY/QQQ/IWM to stop falling",
        "Prime time (before 10:30 NY)": "before 10:30 New York (fixed: gets stricter as the morning goes)",
    }
    need = lambda n: "%s, now %s. Needs %s" % (n, detail.get(n, "?"), NEED.get(n, n.lower()))
    why, flip = [], []
    soft_txt = ("%d weighted point%s missing, at most %d allowed (missing: %s)" % (x["penalty"], "" if x["penalty"] == 1 else "s", x["allowed"],
                ", ".join(s.lower() for s in x["soft_fail"]) or "none"))
    book = x["book"] if x["book"] and x["book"].get("ok") else None
    if book:
        why.append("Level 2: " + book["headline"] + ".")

    if sig in ("BUY NOW", "BUY HALF"):
        why[:0] = ["It broke above the trigger with every MUST check passing (%d/%d checks)." % (x["passed"], len(x["checks"])), soft_txt.capitalize() + "."]
        flip = [["HOLD", "Once filled (limit order, never above %s) the plan freezes: stop %s, half at %s, rest at %s." % (u(mx), u(stop), u(t1), u(target))],
                ["TOO LATE", "If price is already above %s before you're filled. Cancel, don't chase." % u(mx)],
                ["EXIT NOW", "If it trades at %s (stop), or falls under VWAP %s while under your entry." % (u(stop), u(vw))]]
    elif sig in ("HOLD", "SELL HALF") and tr:
        why[:0] = ["You're in at %s since %s NY (%s size). Stop now %s, target %s." % (u(tr["entry"]), tr["opened_ny"], tr["size"], u(tr["stop"]), u(tr["target"]))]
        if sig == "SELL HALF" or tr["half"]:
            why.append("+1R was hit at %s: half is sold and the stop sits at your entry, so the rest is a free trade." % u(tr["half_price"]))
        else:
            flip.append(["SELL HALF", "At %s (+1R): sell half and move the stop up to %s." % (u(tr["t1"]), u(tr["entry"]))])
        flip += [["CLOSED (WIN)", "At %s (+2R target): sell the rest." % u(tr["target"])],
                 ["EXIT NOW", "If it trades at %s (%s)." % (u(tr["stop"]), "breakeven stop" if tr["half"] else "stop-loss")],
                 ["EXIT NOW", "If it falls under VWAP %s%s." % (u(vw), "" if tr["half"] else " while under your entry")]]
        if not tr["half"]:
            flip.append(["EXIT NOW", "If it is still under %s 10 minutes after the entry (no follow-through)." % u(tr["entry"])])
        flip.append(["EXIT NOW", "At 15:55 New York, before the close."])
    elif sig in ("EXIT NOW", "CLOSED") and tr:
        why[:0] = ["Closed at %s: %s. Result: %s %+.1fR." % (u(tr["exit"]), tr["why"], tr["result"], tr["r"] or 0)]
        if (tr["r"] or 0) < 0:
            flip.append(["NO TRADE", "Not again today on this stock: never revenge-trade a loser."])
        else:
            flip.append(["GET READY", "Only after a brand-new pullback pattern forms (new trigger, new stop)."])
        if x["losses"] >= C.MAX_LOSSES:
            flip.append(["NO TRADE", "Daily loss limit reached (%d): no more BUY signals today on any stock." % x["losses"]])
    elif sig == "GET READY":
        why[:0] = ["%s loaded: trigger %s, stop %s (the pullback low)." % (plan["setup"], u(plan["trigger"]), u(stop)),
                   "Every MUST check that can be judged before the breakout passes. " + soft_txt.capitalize() + "."]
        flip = [["BUY NOW", "ALL of: price trades above %s; %s; %s; and it is still at or below %s." % (
                    u(entry), NEED["Breakout on a volume surge"], NEED["Breakout holding (no rejection wick)"], u(mx))],
                ["NO TRADE", "If it breaks out on weak volume or gets slammed from the high (a trap): you'll see NO TRADE instead of BUY."],
                ["TOO LATE", "If it jumps straight past %s." % u(mx)],
                ["WAIT", "If it drops under %s, or the pattern is more than 30 minutes old: the setup is cancelled." % u(plan["stop"])],
                ["NO TRADE", "If it falls under VWAP %s, or dilution news / an SEC offering appears." % u(vw)]]
    elif sig == "TOO LATE":
        why[:0] = ["Price %s is past the max %s, or too close to the target to still offer 1.7:1." % (u(p), u(mx))]
        flip = [["GET READY", "If it pulls back calmly, holds above VWAP %s and forms a new pattern (a new, higher trigger)." % u(vw)],
                ["WAIT", "Meanwhile: never chase. Missing a move costs nothing."]]
    elif sig == "NO TRADE":
        why[:0] = ["Hard stop: " + b + "." for b in x["real_blocks"]] + ["Failed MUST: " + need(n) + "." for n in x["must_fail"]]
        if not x["window_open"]:
            why.append("Outside your trading window (%s-%s New York)." % (C.TRADE_FROM.strftime("%H:%M"), C.TRADE_UNTIL.strftime("%H:%M")))
        if x["reason"] and x["reason"].startswith("not strong"):
            why.append(soft_txt.capitalize() + ".")
        for b in x["real_blocks"]:
            if "dilution" in b or "offering" in b:
                flip.append(["-", "Can't change today: the company is selling shares. Skip this stock."])
            elif "daily limit" in b:
                flip.append(["-", "Tomorrow. The daily loss limit protects your account: stop for today."])
            elif "already" in b:
                flip.append(["-", "Not today for this stock."])
            elif "spread" in b:
                flip.append(["WAIT", "If the real spread tightens under 2%."])
            elif "too few" in b:
                flip.append(["WAIT", "When it trades more (20+ one-minute candles today)."])
            elif "halted" in b:
                flip.append(["WAIT", "When trades resume and prints flow again."])
        for n in x["must_fail"]:
            if n != "No offering / dilution":
                flip.append(["WAIT", "When: " + NEED.get(n, n.lower()) + "."])
        if not x["window_open"] and not x["real_blocks"]:
            flip.append(["WAIT", "When the trading window opens (%s New York)." % C.TRADE_FROM.strftime("%H:%M")])
        if x["reason"] and x["reason"].startswith("not strong"):
            flip.append(["BUY NOW", "On the next breakout, if the weighted misses drop to %d or fewer." % x["allowed"]])
    else:   # WAIT
        pend = x["must_pre"] if x["state"] != "TRIGGERED" else x["must_fail"]
        why[:0] = ["Missing MUST: " + need(n) + "." for n in pend]
        if not x["strong"]:
            why.append("Not strong enough: " + soft_txt + ".")
        if not x["window_open"]:
            why.append("Outside your trading window (%s-%s New York)." % (C.TRADE_FROM.strftime("%H:%M"), C.TRADE_UNTIL.strftime("%H:%M")))
        conds = ([NEED["Clean pullback pattern"]] if not plan and "Clean pullback pattern" not in pend else []) + [NEED.get(n, n.lower()) for n in pend]
        if not x["strong"]:
            conds.append("weighted misses at %d or fewer (fix any of: %s)" % (x["allowed"], ", ".join(s.lower() for s in x["soft_fail"][:4])))
        if not x["window_open"]:
            conds.append("the trading window open (%s New York)" % C.TRADE_FROM.strftime("%H:%M"))
        flip = [["GET READY", "When: " + ("; ".join(dict.fromkeys(conds)) or "the pattern holds for another scan") + "."],
                ["BUY NOW", "Only after GET READY: a break above the trigger on a volume surge that holds."],
                ["NO TRADE", "If dilution news or an SEC offering appears, it falls apart, or the daily loss limit is hit."]]
    uniq = lambda L: [v for i, v in enumerate(L) if v not in L[:i]]
    return dict(means=MEANS.get(sig, ""), why=uniq(why), flip=uniq(flip))

def analyze(inp, now, window_open, mood=None):
    s, bars, p = inp["sym"], inp["bars"], inp["price"]
    fund = inp.get("fund") or {}
    chg = (p / inp["prev_close"] - 1) * 100 if inp["prev_close"] else 0
    vol = sum(b["v"] for b in bars)
    m = now.hour * 60 + now.minute
    hist = inp.get("hist") or []
    same = sum(c[m] for c in hist) / len(hist) if len(hist) >= 3 else 0
    if same > 0:
        rvol, rsrc = vol / same, "vs %d-day avg, same time" % len(hist)
    else:
        exp = inp["avg_vol"] * expected_frac(m) if inp["avg_vol"] else 0
        rvol, rsrc = (vol / exp if exp > 0 else 0), "estimate"
    closes = [b["c"] for b in bars]
    vws = vwap_series(bars)
    vw = vws[-1] if vws else None
    emas = ema(closes, 9)
    e9 = emas[-1] if emas else None
    mac = macd_positive(closes)
    news_all, news_recent = merged_news(inp, now)

    det = detect(bars, p, now)
    st = STATE.get(s)
    state, plan = "WATCH", None
    if det:
        # count how many scans in a row the SAME pattern (same trigger) is seen: one-scan flickers are not setups
        same = st and "fired" not in st and abs(st["trigger"] - det["trigger"]) / det["trigger"] < 0.01
        STATE[s] = dict(det, since=st["since"] if same else time.time(), seen=st.get("seen", 1) + 1 if same else 1)
        state, plan = "ARMED", STATE[s]
    elif st:
        if p < st["stop"] or time.time() - st["since"] > 1800:
            STATE.pop(s, None)
        elif p >= st["trigger"]:
            st.setdefault("fired", time.time())
            state, plan = ("LATE" if time.time() - st["fired"] > 300 else "TRIGGERED"), st
        else:
            state, plan = "ARMED", st

    entry = stop = target = max_price = None
    if plan:
        entry, stop = round(plan["trigger"] + 0.02, 2), round(plan["stop"] - 0.02, 2)
        rng = [b["h"] - b["l"] for b in bars[-11:-1]]
        atr1 = sum(rng) / len(rng) if rng else 0
        if atr1 and entry - stop < atr1:
            stop = round(entry - atr1, 2)      # a stop inside the normal wiggle gets hit by noise, not by a real failure
        if entry - stop >= 0.03:
            target = round(entry + 2 * (entry - stop), 2)
            # never chase: the highest price still worth paying (a quarter of the risk above the entry)
            max_price = round(entry + max(0.03, 0.25 * (entry - stop)), 2)
            if state == "TRIGGERED" and (p > max_price or (p > stop and (target - p) / (p - stop) < 1.7)):
                state = "LATE"
        else:
            plan = entry = stop = None
    if not plan and state != "WATCH":
        state = "WATCH"            # a pattern without a usable stop is not a setup

    # ---- data quality: halts and stale prints (the free feed has no halt flag, so infer it from missing trades)
    iex = FEED == "iex" and not DEMO
    last_t = inp.get("last_t") or (bars[-1]["t"] if bars else None)
    idle = None
    if last_t:
        try:
            idle = (now - parse_t(last_t).astimezone(NY)).total_seconds() / 60
        except Exception:
            idle = None
    regular = dtime(9, 30) <= now.time() < dtime(16)
    halted = bool(not DEMO and regular and idle is not None and idle >= 5)

    q = inp["quote"]
    rq = L2.quote(s)             # the REAL best bid/ask from Schwab beats the IEX sample
    if rq:
        q, iex = rq, False
    spread = (q["ap"] - q["bp"]) / p * 100 if q["ap"] and q["bp"] and p and q["ap"] > q["bp"] else None
    kind = classify(news_recent)
    fl = fund["float"] / 1e6 if fund.get("float") else None
    R = []
    def add(name, pts, mx, detail):
        R.append([name, pts, mx, detail])
    add("Price $2-20", 8 if 2 <= p <= 20 else 0, 8, "$%.2f" % p)
    add("Up 10%+ today", 8 if chg >= 10 else 0, 8, "%+.1f%%" % chg)
    add("Relative volume", 15 if rvol >= 5 else 10 if rvol >= 3 else 5 if rvol >= 2 else 0, 15, "%.1fx (%s)" % (rvol, rsrc))
    add("Low float", 6 if fl is None else 12 if fl < 10 else 8 if fl < 20 else 3 if fl < 50 else 0, 12,
        ("loading..." if not fund.get("loaded") else "unknown (half credit)") if fl is None else "%.2fM shares (%s)" % (fl, fund.get("float_src")))
    nd = {"strong": "strong catalyst: ", "weak": "news: ", "dilution": "DILUTION RISK: ", "none": ""}[kind]
    add("News catalyst", 15 if kind == "strong" else 8 if kind == "weak" else 0, 15, (nd + news_recent[0]["h"][:64]) if news_recent else "none in last 18h")
    add("Above VWAP", 10 if vw and p > vw else 0, 10, "VWAP $%.2f" % vw if vw else "n/a")
    add("Above 9 EMA", 4 if e9 and p > e9 else 0, 4, "EMA9 $%.2f" % e9 if e9 else "n/a")
    add("MACD positive", 4 if mac else 0, 4, "yes" if mac else "no" if mac is not None else "not enough bars")
    risk_pct = (entry - stop) / p * 100 if plan else None
    add("Valid setup + tight stop", 12 if plan and risk_pct <= 10 else 0, 12,
        "%s, risk %.1f%%" % (plan["setup"], risk_pct) if plan else "no pullback or break pattern yet")
    if iex:   # the free feed only shows the IEX book, which is often much wider than the real market: neutral, never a block
        sp_pts = 4 if spread is not None and spread <= 1 else 2
        sp_txt = ("%.2f%% on IEX only (check your broker)" % spread) if spread is not None else "no IEX quote (check your broker)"
    else:
        sp_pts = 4 if spread is not None and spread <= 1 else 2 if spread is not None and spread <= 2 else 0
        sp_txt = "%.2f%%, bid size %s / ask size %s" % (spread, q["bs"], q["as"]) if spread is not None else "n/a"
    add("Tight spread", sp_pts, 4, sp_txt)
    vc = 0
    if plan and plan.get("lightvol"):
        vc += 4
    if plan and state == "TRIGGERED" and plan.get("pullv") and len(bars) >= 2 and max(bars[-1]["v"], bars[-2]["v"]) >= 1.5 * plan["pullv"]:
        vc += 4
    add("Volume confirmation", vc, 8, ("light pullback volume" if plan.get("lightvol") else "heavy pullback volume") +
        (", breakout volume surge" if vc == 8 or (vc == 4 and not plan.get("lightvol")) else "") if plan else "n/a")
    score = sum(r[1] for r in R)
    grade = "A" if score >= 85 else "B" if score >= 70 else "C"

    # ---- SEC filings: the official record, often earlier than the news
    sec = fund.get("sec") or []
    sflags = []
    off = [f for f in sec if f["form"].startswith("424B") and f["age"] <= 10]
    reg = [f for f in sec if f["form"].split("/")[0] in ("S-1", "S-3", "F-1", "F-3", "S-3ASR", "F-3ASR") and f["age"] <= 90]
    eff = [f for f in sec if f["form"] == "EFFECT" and f["age"] <= 30]
    k8 = [f for f in sec if f["form"] in ("8-K", "6-K") and f["age"] <= 1]
    late = [f for f in sec if f["form"].startswith("NT ") and f["age"] <= 120]
    if off: sflags.append(["bad", "Offering filed at the SEC", "%s prospectus on %s: the company is selling shares right now." % (off[0]["form"], off[0]["date"])])
    if eff: sflags.append(["bad", "Shares can be sold any time", "The SEC made a registration effective on %s." % eff[0]["date"]])
    if reg: sflags.append(["warn", "Shelf registration", "%s filed %s: shares ready to sell into rallies." % (reg[0]["form"], reg[0]["date"])])
    if late: sflags.append(["warn", "Late financial report", "%s on %s: the company missed a deadline." % (late[0]["form"], late[0]["date"])])
    if k8: sflags.append(["good", "Fresh %s filed" % k8[0]["form"], "Filed %s: a material event that may explain the move. Worth reading." % k8[0]["date"]])

    blocks = [] if window_open else ["outside the trading window"]
    if kind == "dilution":
        blocks.append("offering / dilution news")
    if off:
        blocks.append("SEC offering prospectus filed in the last 10 days")
    if spread is not None and spread > 2 and not iex:
        blocks.append("spread over 2%")
    if len(bars) < 20:
        blocks.append("too few trades on the data feed today (thin stock)")
    if halted:
        blocks.append("no trades for %d minutes (possibly halted)" % idle)
    # ---- key price levels: where buyers and sellers are likely to show up
    hod = max(b["h"] for b in bars) if bars else p
    pm = [b["h"] for b in bars if parse_t(b["t"]).astimezone(NY).time() < dtime(9, 30)]
    daily = inp.get("daily") or []
    raw = []
    def L(label, price, why):
        if price and price > 0:
            raw.append([label, round(price, 2), why])
    L("High of day", hod, "Today's highest price. Breaking it often brings a new wave of buyers.")
    if pm and now.time() >= dtime(9, 30):
        L("Pre-market high", max(pm), "Highest price before the open. Traders watch it closely.")
    L("VWAP", vw, "Average price paid today. Above = buyers in control, below = sellers.")
    if daily:
        L("Yesterday's high", daily[-1]["h"], "People who bought there yesterday may sell to break even.")
        L("Yesterday's close", daily[-1]["c"], "Where it started today. Losing it erases the day's gain.")
    if len(daily) >= 10:
        L("%d-day high" % len(daily), max(b["h"] for b in daily), "Highest price in the last few weeks: strong resistance.")
    L("52-week high", fund.get("hi52"), "Highest price in a year. Above it there are few sellers left (blue sky).")
    if p >= 1:
        for lvl in (math.floor(p) + 0.5 if p - math.floor(p) < 0.5 else None, math.floor(p) + 1, math.ceil(p) - 0.5 if p - math.floor(p) > 0.5 else None):
            if lvl and lvl > p and (lvl - p) / p < 0.08:
                L("Round number", lvl, "Whole and half dollars attract many orders, so price often stalls there.")
    levels = []
    for lab, pr, why in sorted(raw, key=lambda x: -x[1]):
        if any(abs(pr - x["price"]) / pr < 0.004 for x in levels):
            continue
        levels.append(dict(label=lab, price=pr, why=why, dist=round((pr / p - 1) * 100, 1), side="res" if pr > p else "sup"))
    res = [x for x in levels if x["side"] == "res"]
    sup = [x for x in levels if x["side"] == "sup"]
    next_res = res[-1] if res else None
    next_sup = sup[0] if sup else None
    # first real resistance ABOVE the entry (the trigger itself is not resistance once broken)
    res_above = None
    if entry:
        above = [x for x in levels if x["price"] > entry * 1.004 and x["label"] != "Round number"]
        res_above = min(above, key=lambda x: x["price"]) if above else None
    room_r = round((res_above["price"] - entry) / (entry - stop), 1) if res_above and entry and stop and entry > stop else None

    # ---- float rotation (full-market volume from Yahoo / float)
    yq = inp.get("yq") or {}
    fullvol = yq.get("vol") if yq.get("day") == now.date().isoformat() else None
    rotation = round(fullvol / fund["float"], 2) if fullvol and fund.get("float") else None

    # ---- THE DECISION: a strict checklist. Every MUST item has to pass, or there is no trade.
    ext = (p / vw - 1) * 100 if vw else None
    stable = bool(plan) and plan.get("seen", 1) >= 2
    def vpace(b):
        try:
            el = (now - parse_t(b["t"]).astimezone(NY)).total_seconds()
        except Exception:
            return b["v"]
        return b["v"] * 60 / max(15, el) if 0 <= el < 60 else b["v"]
    bvol = bool(plan and plan.get("pullv") and len(bars) >= 2 and max(vpace(bars[-1]), bars[-2]["v"]) >= 1.5 * plan["pullv"])
    late_morning = m >= 630 and not DEMO
    # buying pressure: volume on green candles vs red candles over the last 10 minutes
    up = sum(b["v"] for b in bars[-10:] if b["c"] >= b["o"]); dn = sum(b["v"] for b in bars[-10:] if b["c"] < b["o"])
    press = None if len(bars) < 10 else (up / dn if dn else 9.9)
    # higher timeframe: 5-minute closes above their own 9 EMA
    c5 = [bars[i]["c"] for i in range(len(bars) - 1, -1, -5)][::-1]
    tf5 = None if len(c5) < 6 else bool(c5[-1] > ema(c5, 9)[-1])
    # breakout candle: price must hold near the top of the candle, not spike and get slammed (an upper wick = trap)
    lb = bars[-1] if bars else None
    hold_ok = True
    if state == "TRIGGERED" and lb and lb["h"] > lb["l"] and lb["h"] >= (plan or {}).get("trigger", 1e9):
        hold_ok = (lb["h"] - p) <= 0.5 * (lb["h"] - lb["l"])
    yq0 = inp.get("yq") or {}
    fv0 = yq0.get("vol") if yq0.get("day") == now.date().isoformat() else None
    dollars = fv0 * p if fv0 else None
    # Level 2: the live order book, read against this plan (None when not connected: then it never counts against the trade)
    t1v = round(entry + (entry - stop), 2) if entry and stop else None
    book = L2.read(s, dict(price=p, entry=entry, stop=stop, max_price=max_price, t1=t1v, vwap=vw)) if L2.enabled else None
    bk = book["check"] if book and book.get("ok") else (None, "not connected" if not L2.public_status()["live"] else "no book yet")
    CK = []
    def ck(name, ok, must, detail, w=0):
        CK.append([name, None if ok is None else bool(ok), must, detail, w])
    # MUST: any fail = no trade
    ck("Volume 3x+ normal", rvol >= 3, True, "%.1fx" % rvol)
    ck("Above VWAP", vw and p > vw, True, "VWAP $%.2f" % vw if vw else "n/a")
    ck("No offering / dilution", kind != "dilution" and not off, True, "dilution news" if kind == "dilution" else "SEC offering filed" if off else "clean")
    ck("Clean pullback pattern", stable, True, (plan["setup"] if stable else "forming...") if plan else "none yet")
    ck("Tight stop (8% or less)", plan and risk_pct <= 8, True, "%.1f%%" % risk_pct if plan else "no stop yet")
    ck("Room to run (1R+ to resistance)", room_r is None or room_r >= 1, True,
       "%s %.1fR away" % (res_above["label"], room_r) if room_r is not None else "clear")
    ck("Liquid ($1M+ traded today)", None if dollars is None else dollars >= 1e6, True, "unknown" if dollars is None else "$%.1fM" % (dollars / 1e6))
    ck("Breakout on a volume surge", bvol, True, "yes" if bvol else "checked at the breakout")
    ck("Breakout holding (no rejection wick)", hold_ok, True, "yes" if hold_ok else "sellers slammed the breakout")
    # KEY (2 points each)
    ck("Real news catalyst", kind == "strong", False, {"strong": "strong", "weak": "weak news", "dilution": "dilution", "none": "no news"}[kind], 2)
    ck("Low float (under 20M)", None if fl is None else fl < 20, False, "unknown" if fl is None else "%.1fM" % fl, 2)
    ck("Buyers in control (last 10 min)", None if press is None else press >= 1.2, False, "n/a" if press is None else "%.1fx up vs down volume" % press, 2)
    ck("5-minute trend up", tf5, False, {True: "yes", False: "no", None: "not enough data"}[tf5], 2)
    if L2.enabled:
        ck("Order book with you (Level 2)", bk[0], False, bk[1], 2)
    hs = SETUP_HIST.get(plan["setup"]) if plan else None
    if hs and hs["n"] >= 8:
        ck("This setup has an edge (past results)", hs["r"] > 0, False, "%dW/%dL, %+.1fR over %d trades" % (hs["w"], hs["n"] - hs["w"], hs["r"], hs["n"]), 2)
    # MINOR (1 point each)
    ck("Up 10%+ today", chg >= 10, False, "%+.1f%%" % chg, 1)
    ck("Trend up (9 EMA + MACD)", None if mac is None else bool(e9 and p > e9 and mac), False, "n/a" if mac is None else "yes" if e9 and p > e9 and mac else "no", 1)
    ck("Quiet pullback volume", plan and plan.get("lightvol"), False, "light" if plan and plan.get("lightvol") else "heavy" if plan else "n/a", 1)
    ck("Not stretched (<20% over VWAP)", ext is None or ext <= 20, False, "n/a" if ext is None else "%+.0f%% vs VWAP" % ext, 1)
    ck("Market not weak", None if not mood else mood.get("tone") != "bad", False, (mood or {}).get("tone") or "unknown", 1)
    ck("Prime time (before 10:30 NY)", not late_morning, False, now.strftime("%H:%M NY"), 1)
    must_fail = [c[0] for c in CK if c[2] and c[1] is False]
    must_pre = [x for x in must_fail if x not in ("Breakout on a volume surge", "Breakout holding (no rejection wick)")]
    soft_fail = [c[0] for c in sorted(CK, key=lambda c: -c[4]) if not c[2] and c[1] is False]
    penalty = sum(c[4] for c in CK if not c[2] and c[1] is False)
    allowed = 1 if hs and hs["n"] >= 8 and hs["r"] <= 0 else 2     # a setup that has been losing gets stricter
    strong = penalty <= allowed
    passed = sum(1 for c in CK if c[1])

    tl = trades_today(now)
    lost_here = any(t["sym"] == s and t.get("closed") and t["r"] < 0 for t in tl)
    n_here = sum(1 for t in tl if t["sym"] == s)
    losses = sum(1 for t in tl if t.get("closed") and t["r"] < 0)
    real_blocks = [b for b in blocks if "window" not in b]
    if losses >= C.MAX_LOSSES:
        real_blocks.append("%d losing trades today: daily limit reached, stop trading" % losses)
    elif lost_here:
        real_blocks.append("already lost on %s today: never revenge-trade the same stock" % s)
    elif n_here >= C.MAX_TRADES_PER_STOCK:
        real_blocks.append("already traded %s %d times today" % (s, n_here))
    reason = None
    if state == "TRIGGERED":
        if real_blocks:
            sig, reason = "NO TRADE", real_blocks[0]
        elif not window_open:
            sig, reason = "NO TRADE", "outside the trading window"
        elif must_fail:
            sig, reason = "NO TRADE", "failed: " + must_fail[0].lower()
        elif strong:
            sig = "BUY NOW"
        else:
            sig, reason = "NO TRADE", "not strong enough: missing " + ", ".join(x.lower() for x in soft_fail[:3])
    elif state == "ARMED":
        if real_blocks:
            sig, reason = "NO TRADE", real_blocks[0]
        elif window_open and not must_pre and strong:
            sig = "GET READY"
        else:
            sig = "WAIT"
            reason = ("waiting for: " + must_pre[0].lower()) if must_pre else "not strong enough: missing " + ", ".join(x.lower() for x in soft_fail[:3]) \
                if not strong else "outside the trading window"
    elif state == "LATE" and not real_blocks:
        sig, reason = "TOO LATE", "it already ran past the entry"
    else:
        sig = "NO TRADE" if real_blocks else "WAIT"
        reason = real_blocks[0] if real_blocks else ("waiting for: " + must_pre[0].lower()) if must_pre else "waiting for a clean pullback"
    blocks = blocks + [b for b in real_blocks if b not in blocks]

    # ---- trade lock: once a BUY is called, the plan is FROZEN and the app manages the trade (no flip-flopping)
    tr = next((t for t in reversed(tl) if t["sym"] == s), None)
    if tr and not tr.get("closed"):
        manage_trade(tr, bars, p, vw, now)
    elif sig in ("BUY NOW", "BUY HALF") and plan:
        tr = open_trade(s, sig, entry, stop, max_price, p, now, plan["setup"])
    since_close = now.timestamp() - tr["closed"] if tr and tr.get("closed") else None
    if tr and (since_close is None or since_close < 180 or (since_close < 1800 and sig in ("WAIT", "NO TRADE", "TOO LATE"))):
        entry, stop, target, max_price = tr["entry"], tr["stop"], tr["target"], tr["max_price"]
        sig, reason = trade_signal(tr, now)
    action = action_line(sig, reason, entry, stop, target, max_price, tr, p)
    explain = explain_signal(dict(sig=sig, reason=reason, state=state, plan=plan, entry=entry, stop=stop, target=target, max_price=max_price,
                                  tr=tr, p=p, vw=vw, rvol=rvol, risk_pct=risk_pct, room_r=room_r, res_above=res_above, dollars=dollars,
                                  must_fail=must_fail, must_pre=must_pre, soft_fail=soft_fail, penalty=penalty, allowed=allowed, strong=strong,
                                  real_blocks=real_blocks, window_open=window_open, checks=CK, passed=passed, book=book, losses=losses))

    # ---- tips: [tone, short title, one-line detail]. tone: bad = deal-breaker, good = in your favor, warn = be careful
    T = []
    def tip(k, title, detail):
        T.append([k, title, detail])
    if halted:
        tip("bad", "No trades for %d min" % idle, "It may be halted, or very thin on the free feed. Don't trade it until prints come back.")
    if len(bars) < 20:
        tip("bad", "Too few trades", "Prices can jump around on so little volume. Not safe to trade.")
    if kind == "dilution":
        tip("bad", "Company is selling shares", "Offering / dilution news. Sellers will cap every bounce. Stay away.")
    elif kind == "strong":
        tip("good", "Real news catalyst", "News like this keeps buyers coming back all morning.")
    elif kind == "weak":
        tip("warn", "Weak news", "Not a strong reason for the move. Take profits faster than usual.")
    else:
        tip("warn", "No news", "No headline in 18 hours. Moves without a reason fade fast: quick trade only.")
    for f in sflags:
        tip(f[0], "SEC: " + f[1], f[2])
    if vw:
        if p >= vw:
            tip("good", "Above VWAP $%.2f" % vw, "Buyers are in control. The idea weakens if it falls back under VWAP.")
        else:
            tip("bad", "Below VWAP $%.2f" % vw, "Sellers are in control. Wait until it climbs back above VWAP.")
    if rvol >= 10:
        tip("good", "Huge volume (%s)" % ("100x+" if rvol > 100 else "%.0fx" % rvol), "Today's crowd favorite. Expect big swings both ways.")
    elif rvol < 2:
        tip("warn", "Normal volume (%.1fx)" % rvol, "No crowd yet. Breakouts often fail without one.")
    if rotation is not None and rotation >= 1:
        tip("good", "Float rotated %.1fx" % rotation, "Every tradable share changed hands today: extreme demand, wild swings.")
    if res_above and room_r is not None:
        if room_r < 1:
            tip("bad" if room_r < 0.5 else "warn", "Resistance right above entry",
                "%s at $%.2f is only %.1fR away. Little room: take quick profits there or skip." % (res_above["label"], res_above["price"], room_r))
        elif target and res_above["price"] < target:
            tip("warn", "Resistance before the target",
                "%s at $%.2f. Sell half there and move your stop to the entry." % (res_above["label"], res_above["price"]))
    if hod and hod * 0.98 <= p < hod:
        tip("good", "Near the high of day", "Within 2%% of $%.2f. A new high often brings fresh buyers." % hod)
    elif hod and p < hod * 0.85:
        tip("warn", "Faded from the high", "%.0f%% below today's high ($%.2f). The big move may be over." % ((1 - p / hod) * 100, hod))
    hi52 = fund.get("hi52")
    if hi52:
        if p >= hi52:
            tip("good", "New 52-week high", "Nobody who bought in the last year is losing money: fewer sellers above.")
        elif p >= hi52 * 0.95:
            tip("good", "Near the 52-week high", "A break above $%.2f can trigger a fast move (blue sky)." % hi52)
    sp = fund.get("short_pct")
    if sp and sp >= 15 and (fl is None or fl < 30):
        tip("good", "High short interest (%.0f%%)" % sp, "If it keeps rising, short sellers may have to buy back (squeeze).")
    if fl is not None and fl < 5:
        tip("good", "Tiny float (%.2fM)" % fl, "It can double fast, and drop just as fast.")
    if chg >= 60:
        tip("warn", "Very extended (+%.0f%%)" % chg, "Only buy clean pullbacks and expect sharp drops.")
    if plan and plan.get("lightvol") is False:
        tip("warn", "Heavy selling on the pullback", "Wait for the breakout to come with strong volume.")
    if plan and risk_pct and risk_pct > 6:
        tip("warn", "Wide stop (%.1f%%)" % risk_pct, "Use fewer shares. The share calculator already adjusts for it.")
    if spread is not None and iex and spread > 2:
        tip("warn", "Check the real spread", "The free feed shows %.1f%%. If your broker shows more than 1-2%%, skip it." % spread)
    elif spread is not None and spread > 1:
        tip("warn", "Wide spread (%.1f%%)" % spread, "Limit orders only, never market orders.")
    if fund.get("country") in RISKY_COUNTRIES:
        tip("warn", "Based in %s" % fund["country"], "Small caps from this region have had sudden crashes. Small size, quick profits.")
    mc = fund.get("mcap")
    if mc and mc < 50e6:
        tip("warn", "Micro-cap ($%.0fM)" % (mc / 1e6), "The tiniest companies are the most volatile.")
    ed = earnings_in_days(fund.get("earnings"), now.date())
    if ed is not None and 0 <= ed <= 7:
        tip("warn", "Earnings %s" % ("today" if ed == 0 else "in %d day%s" % (ed, "" if ed == 1 else "s")), "Expect big gaps around the report.")
    if 630 <= m < 660 and window_open:
        tip("warn", "Late morning", "After 10:30 New York time momentum usually fades. Be pickier.")
    T.sort(key=lambda t: {"bad": 0, "good": 1, "warn": 2}[t[0]])
    r0 = (tr["entry"] - tr["stop0"]) if tr and tr.get("stop0") is not None and entry == tr["entry"] else (entry - stop) if entry and stop else None
    rr = round((target - entry) / r0, 1) if target and entry and r0 and r0 > 0 else None

    # ---- Ross's 5 pillars
    pillars = [["Price $2 to $20", 2 <= p <= 20, "$%.2f" % p, "Cheap enough to move fast in % terms."],
               ["Up 10% or more today", chg >= 10, "%+.1f%%" % chg, "Already showing strength today."],
               ["Relative volume 5x+", rvol >= 5, "%.1fx" % rvol if rvol <= 100 else "100x+", "The crowd is here."],
               ["Fresh news", kind == "strong" or (None if kind == "weak" else False), {"strong": "strong", "weak": "weak", "dilution": "dilution!", "none": "none"}[kind], "A reason for the move."],
               ["Float under 20M", None if fl is None else fl < 20, "?" if fl is None else "%.1fM" % fl, "Small supply moves fast."]]

    bits = []
    if kind == "strong": bits.append("news catalyst")
    if kind == "dilution" or off: bits.append("dilution risk")
    if rvol >= 5: bits.append("100x+ volume" if rvol > 100 else "%.0fx volume" % rvol)
    if fl is not None and fl < 10: bits.append(("low float %.2fM" if fl < 1 else "low float %.1fM") % fl)
    if vw and p < vw: bits.append("below VWAP")
    if halted: bits.append("possibly halted")
    if plan: bits.append(plan["setup"].lower())
    if state == "LATE": bits.append("already ran")
    why = ", ".join(bits) or "moving, no clear edge yet"
    if sig == "NO TRADE" and real_blocks:
        why = "avoid: " + real_blocks[0]

    tail = bars[-60:]
    return dict(sym=s, price=round(p, 2), chg=round(chg, 1), rvol=round(rvol, 1), float_m=fl, spread=None if spread is None else round(spread, 2),
                spread_iex=iex, vwap=vw and round(vw, 2), ema9=e9 and round(e9, 2), score=score, grade=grade, signal=sig, state=state,
                action=action, reason=reason, checks=CK, passed=passed, total=sum(1 for c in CK if c[1] is not None), penalty=penalty, allowed=allowed,
                trade=tr and {k: tr.get(k) for k in TRADE_PUBLIC},
                setup=plan["setup"] if plan else None, note=plan.get("note") if plan else None,
                trigger=plan and round(plan["trigger"], 2), entry=entry, stop=stop, target=target, max_price=max_price, blocks=blocks, rr=rr,
                halted=halted, idle_min=None if idle is None else round(idle, 1), room_r=room_r, res_above=res_above,
                hod=round(hod, 2), tips=T, why=why, pillars=pillars, levels=levels, next_res=next_res, next_sup=next_sup,
                rotation=rotation, fullvol=fullvol, sec=sec[:12], sec_flags=sflags,
                fund={k: fund.get(k) for k in ("loaded", "sources", "name", "country", "sector", "industry", "employees", "summary", "website",
                                               "mcap", "hi52", "lo52", "float", "float_src", "shares", "short_pct", "short_ratio",
                                               "inst_pct", "insider_pct", "earnings", "avg10")},
                rules=R, news=news_all, news_kind=kind, explain=explain,
                book_line=book["headline"] if book and book.get("ok") and book.get("fresh") else None,
                candles=[[int(parse_t(b["t"]).timestamp() * 1000), round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4), b["v"]] for b in tail],
                vwap_s=[v and round(v, 4) for v in vws[-60:]], ema_s=[round(v, 4) for v in emas[-60:]])

def earnings_in_days(txt, today):
    """Days until the next earnings date, from Yahoo ('2026-11-25') or Finviz ('Nov 25 AMC'). None if unknown."""
    if not txt:
        return None
    txt = str(txt).strip()
    for fmt, part in (("%Y-%m-%d", txt[:10]), ("%b %d", " ".join(txt.split()[:2]))):
        try:
            d = datetime.strptime(part, fmt).date()
            if fmt == "%b %d":
                d = d.replace(year=today.year)
                if (today - d).days > 180:
                    d = d.replace(year=today.year + 1)
            return (d - today).days
        except ValueError:
            continue
    return None

# ---------------------------------------------------------------- trade manager
# Once a BUY is called, its entry/stop/target are frozen and the app follows the trade to the exit:
# HOLD -> SELL HALF at +1R (stop moves to breakeven) -> TARGET at +2R, or EXIT at the stop / on a failed breakout.
# Every result is recorded, so the scoreboard shows the app's real hit rate (no hiding losers).
TRADES = {"day": None, "list": []}
TRADE_PUBLIC = ("id", "sym", "size", "setup", "opened_ny", "entry", "stop0", "stop", "t1", "target", "max_price",
                "half", "half_price", "closed", "closed_ny", "exit", "result", "why", "r", "best")
TLOCK = threading.RLock()

SETUP_HIST = {}
def load_setup_hist():
    """Win/loss record per setup type from every past trade, so setups that keep losing get stricter rules."""
    SETUP_HIST.clear()
    try:
        with open(C.TRADE_LOG, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                h = SETUP_HIST.setdefault(row["setup"], dict(n=0, w=0, r=0.0))
                h["n"] += 1; h["w"] += row["result"] == "WIN"; h["r"] = round(h["r"] + float(row["R"] or 0), 2)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("could not read the trade log: %s", e)

def trades_today(now):
    with TLOCK:
        d = now.date().isoformat()
        if TRADES["day"] != d:
            TRADES.update(day=d, list=[])
            if not DEMO:
                try:
                    with open(C.TRADES_FILE, encoding="utf-8") as f:
                        st = json.load(f)
                    if st.get("day") == d:
                        TRADES["list"] = st.get("list", [])
                except FileNotFoundError:
                    pass
                except Exception as e:
                    log.warning("could not read trades: %s", e)
        return TRADES["list"]

def _trades_save():
    if DEMO:
        return
    try:
        tmp = C.TRADES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(TRADES, f)
        os.replace(tmp, C.TRADES_FILE)
    except Exception:
        log.exception("could not save trades")

def open_trade(sym, sig, entry, stop, max_price, p, now, setup):
    fill = round(min(max(entry, p), max_price), 2)     # realistic fill: your limit order, never above the max price
    risk = fill - stop
    tr = dict(id="%s-%s" % (sym, now.strftime("%H%M%S")), sym=sym, size="half" if sig == "BUY HALF" else "full", setup=setup,
              opened=now.timestamp(), opened_ny=now.strftime("%H:%M"), entry=fill, stop0=stop, stop=stop,
              t1=round(fill + risk, 2), target=round(fill + 2 * risk, 2), max_price=max_price,
              half=False, half_at=None, half_price=None, closed=None, closed_ny=None, exit=None, result=None, why=None, r=None, best=fill)
    with TLOCK:
        trades_today(now).append(tr)
        _trades_save()
    log.info("TRADE OPEN %s %s @ %.2f stop %.2f target %.2f", sym, tr["size"], fill, stop, tr["target"])
    return tr

def _close(tr, price, why, now):
    risk = tr["entry"] - tr["stop0"]
    move = (price - tr["entry"]) / risk if risk > 0 else 0
    tr["r"] = round(0.5 * 1 + 0.5 * move if tr["half"] else move, 2)
    tr.update(closed=now.timestamp(), closed_ny=now.strftime("%H:%M"), exit=round(price, 2), why=why,
              result="WIN" if tr["r"] > 0.05 else "LOSS" if tr["r"] < -0.05 else "BREAKEVEN")
    log.info("TRADE CLOSED %s %s %+.2fR (%s)", tr["sym"], tr["result"], tr["r"], why)
    if DEMO:
        return
    try:
        new = not os.path.exists(C.TRADE_LOG)
        with open(C.TRADE_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "symbol", "size", "setup", "opened", "entry", "stop", "target", "half_at_1R", "closed", "exit", "result", "R", "why"])
            w.writerow([now.date(), tr["sym"], tr["size"], tr["setup"], tr["opened_ny"], tr["entry"], tr["stop0"], tr["target"],
                        "yes" if tr["half"] else "no", tr["closed_ny"], tr["exit"], tr["result"], tr["r"], why])
        load_setup_hist()
    except Exception:
        log.exception("could not write the trade log")

def manage_trade(tr, bars, p, vw, now):
    """Walk every candle since the entry (so a wick between two scans still counts), then the live price."""
    if tr.get("closed"):
        return
    with TLOCK:
        nowts = now.timestamp()
        pts = []
        for b in bars:
            t = parse_t(b["t"]).timestamp()
            if t >= tr["opened"] and not (tr["half"] and t < tr["half_at"]):
                pts.append((t, b["h"], b["l"]))
        pts.append((nowts, p, p))
        changed = False
        for t, h, l in pts:
            tr["best"] = max(tr.get("best") or 0, h)
            if l <= tr["stop"]:
                _close(tr, tr["stop"], "back to entry after selling half" if tr["half"] else "stop-loss hit", now); changed = True
                break
            if h >= tr["target"]:
                _close(tr, tr["target"], "target hit (+2R)", now); changed = True
                break
            if not tr["half"] and h >= tr["t1"]:
                # later candles that started before this moment may contain older lows: ignore them for the new stop
                tr.update(half=True, half_price=tr["t1"], half_at=nowts if t >= nowts else min(t + 60, nowts), stop=tr["entry"]); changed = True
        if not tr.get("closed"):
            age = nowts - tr["opened"]
            if vw and p < vw and (tr["half"] or p < tr["entry"]):
                _close(tr, p, "lost VWAP: sellers took control", now); changed = True
            elif not tr["half"] and age >= 600 and p < tr["entry"]:
                _close(tr, p, "no follow-through after 10 minutes", now); changed = True
            elif not DEMO and now.time() >= dtime(15, 55):
                _close(tr, p, "market closing", now); changed = True
        if changed:
            _trades_save()

def trade_signal(tr, now):
    nowts = now.timestamp()
    if tr.get("closed"):
        if nowts - tr["closed"] < 180 and "target" not in tr["why"]:
            return "EXIT NOW", tr["why"]
        return "CLOSED", tr["why"]
    if tr["half"] and nowts - tr["half_at"] < 180:
        return "SELL HALF", "hit +1R"
    return "HOLD", None

def action_line(sig, reason, entry, stop, target, max_price, tr, p):
    """The one sentence that tells you exactly what to do."""
    u = lambda v: "$%.2f" % v if v is not None else "-"
    if tr and sig in ("HOLD", "SELL HALF", "EXIT NOW", "CLOSED"):
        if sig == "SELL HALF":
            return "SELL HALF now (about %s). Move your stop UP to %s (breakeven). Hold the rest for %s." % (u(tr["half_price"]), u(tr["entry"]), u(tr["target"]))
        if sig == "HOLD":
            if tr["half"]:
                return "HOLD the rest. Stop is now %s (you can't lose on this trade). Sell the rest at %s." % (u(tr["stop"]), u(tr["target"]))
            return "HOLD. Stop-loss %s. Sell half at %s, the rest at %s. Exit if it falls under VWAP." % (u(tr["stop"]), u(tr["t1"]), u(tr["target"]))
        res = "%s %+.1fR" % (tr["result"], tr["r"])
        if sig == "EXIT NOW":
            return "SELL EVERYTHING NOW (about %s): %s. Result: %s." % (u(tr["exit"]), tr["why"], res)
        return "Trade done at %s: %s. Result: %s. Wait for a new setup." % (u(tr["exit"]), tr["why"], res)
    if sig in ("BUY NOW", "BUY HALF"):
        t1 = entry + (entry - stop)
        return "BUY NOW: limit order, never pay above %s. Stop-loss %s. Sell half at %s, rest at %s." % (u(max_price), u(stop), u(t1), u(target))
    if sig == "GET READY":
        return "DON'T BUY YET. Wait for the BUY alert (it comes only if it breaks %s the right way)." % u(entry)
    if sig == "TOO LATE":
        return "DON'T CHASE. It ran past the entry. Wait for the next pullback."
    if sig == "NO TRADE":
        return "DON'T BUY: %s." % (reason or "rules not met")
    return "DON'T BUY YET: %s." % (reason or "no setup")

def trade_summary(now):
    tl = trades_today(now)
    done = [t for t in tl if t.get("closed")]
    w = sum(1 for t in done if t["result"] == "WIN")
    l = sum(1 for t in done if t["result"] == "LOSS")
    return dict(list=[{k: t.get(k) for k in TRADE_PUBLIC} for t in reversed(tl)], open=len(tl) - len(done), wins=w, losses=l,
                be=len(done) - w - l, r=round(sum(t["r"] for t in done), 2), max_losses=C.MAX_LOSSES)

# ---------------------------------------------------------------- live data (Alpaca)
BARS, BARS_DAY, DAILY, DAYBARS, NEWS = {}, [None], {}, {}, [0, {}]

def bars_multi(syms, tf, start):
    res, tok = {s: [] for s in syms}, None
    for _ in range(60):
        p = dict(symbols=",".join(syms), timeframe=tf, start=start, limit=10000, feed=FEED, adjustment="raw", sort="asc")
        if tok:
            p["page_token"] = tok
        r = alp("/v2/stocks/bars", **p)
        for s, b in (r.get("bars") or {}).items():
            res.setdefault(s, []).extend(b)
        tok = r.get("next_page_token")
        if not tok:
            break
    return res

def update_bars(syms):
    day = today_start().date()
    if BARS_DAY[0] != day:
        BARS.clear(); BARS_DAY[0] = day
    new = [s for s in syms if not BARS.get(s)]
    old = [s for s in syms if BARS.get(s)]
    groups = []
    if new:
        groups.append((new, today_start().isoformat()))
    if old:
        groups.append((old, min(BARS[s][-1]["t"] for s in old)))
    for g, start in groups:
        for s, bl in bars_multi(g, "1Min", start).items():
            cur = BARS.setdefault(s, [])
            for b in bl:
                if cur and b["t"] == cur[-1]["t"]:
                    cur[-1] = b
                elif not cur or b["t"] > cur[-1]["t"]:
                    cur.append(b)

HIST = {}
def hist_load(syms):
    today = datetime.now(NY).date()
    need = [s for s in syms if s not in HIST or HIST[s][0] != today]
    if not need:
        return
    try:
        start = (datetime.now(NY) - timedelta(days=16)).replace(hour=4, minute=0, second=0, microsecond=0).isoformat()
        d = bars_multi(need, "1Min", start)
    except Exception:
        d = {}
    for s in need:
        days = {}
        for b in d.get(s, []):
            t = parse_t(b["t"]).astimezone(NY)
            if t.date() < today:
                days.setdefault(t.date(), [0] * 1440)[t.hour * 60 + t.minute] += b["v"]
        curves = []
        for k in sorted(days):
            acc, cum = 0, []
            for v in days[k]:
                acc += v
                cum.append(acc)
            curves.append(cum)
        HIST[s] = (today, curves[-10:])

def daily_load(syms):
    today = datetime.now(NY).date()
    need = [s for s in syms if s not in DAILY or DAILY[s][0] != today]
    if not need:
        return
    start = (datetime.now(NY) - timedelta(days=45)).date().isoformat()
    d = bars_multi(need, "1Day", start)
    for s in need:
        prior = [b for b in d.get(s, []) if parse_t(b["t"]).astimezone(NY).date() < today]
        vols = [b["v"] for b in prior][-20:]
        DAILY[s] = (today, sum(vols) / len(vols) if vols else 0)
        DAYBARS[s] = prior[-30:]

def news_map(syms):
    if time.time() - NEWS[0] > 60:
        start = (datetime.now(NY) - timedelta(hours=48)).isoformat()
        r = alp("/v1beta1/news", symbols=",".join(syms), start=start, limit=50, sort="desc")
        m = {}
        for n in r.get("news", []):
            for s in n.get("symbols", []):
                m.setdefault(s, []).append(dict(h=n.get("headline", ""), t=n.get("created_at", ""), url=n.get("url", ""), src="Benzinga"))
        NEWS[0], NEWS[1] = time.time(), m
    return NEWS[1]

ASSETS = {}
FUND_RX = re.compile(r"\b(ETF|ETN|ETP|Fund|Trust|Leveraged|Inverse|[23]X|Ultra(Pro|Short)?|Daily Target|Bull|Bear)\b", re.I)
def asset_name(s):
    """Company name from Alpaca (cached). Used to drop ETFs/funds, which are not momentum stocks."""
    if s not in ASSETS:
        try:
            ASSETS[s] = paper("GET", "/assets/" + urllib.parse.quote(s)).get("name") or ""
        except Exception:
            ASSETS[s] = ""
    return ASSETS[s]

def prev_close_of(sn, now):
    db, pdb = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
    if db.get("t") and parse_t(db["t"]).astimezone(NY).date() < now.date():
        return db.get("c")
    return pdb.get("c")

def fetch_live():
    now = datetime.now(NY)
    keep_syms = set(WATCH) | {t["sym"] for t in trades_today(now) if not t.get("closed")}
    syms = set(keep_syms)
    mv = alp("/v1beta1/screener/stocks/movers", top=50)
    for g in mv.get("gainers", []):
        if 1.5 <= g.get("price", 0) <= 25 and g.get("percent_change", 0) >= 8:
            syms.add(g["symbol"])
    try:
        for a in alp("/v1beta1/screener/stocks/most-actives", by="volume", top=50).get("most_actives", []):
            syms.add(a["symbol"])
    except Exception:
        pass
    syms = {s for s in syms if "." not in s or s in keep_syms}
    if not syms:
        return []
    snaps = alp("/v2/stocks/snapshots", symbols=",".join(sorted(syms)[:100]), feed=FEED)
    snaps = snaps.get("snapshots", snaps)
    cand = []
    for s, sn in snaps.items():
        try:
            p, prev = sn["latestTrade"]["p"], prev_close_of(sn, now)
            chg = (p / prev - 1) * 100
        except (KeyError, TypeError, ZeroDivisionError):
            continue
        if (1.5 <= p <= 25 and chg >= 8) or s in keep_syms:
            cand.append((chg, s, sn, prev))
    keep = []
    for c in sorted(cand, key=lambda c: (c[1] not in keep_syms, -c[0])):
        if c[1] in keep_syms or not FUND_RX.search(asset_name(c[1])):
            keep.append(c)
        if len(keep) >= 25:
            break
    cand = keep
    if not cand:
        return []
    names = [c[1] for c in cand]
    WANT.clear(); WANT.update(names)
    update_bars(names); daily_load(names); hist_load(names); nm = news_map(names)
    out = []
    for chg, s, sn, prev in cand:
        if not BARS.get(s):
            continue
        q = sn.get("latestQuote") or {}
        out.append(dict(sym=s, price=sn["latestTrade"]["p"], prev_close=prev, bars=BARS[s], avg_vol=DAILY.get(s, (0, 0))[1],
                        hist=HIST.get(s, (0, []))[1], daily=DAYBARS.get(s, []), fund=fundamentals(s), yq=YQ.get(s), news=nm.get(s, []),
                        quote=dict(ap=q.get("ap"), bp=q.get("bp"), **{"as": q.get("as"), "bs": q.get("bs")}),
                        last_t=(sn.get("latestTrade") or {}).get("t")))
    return out

LOGGED = set()
LOG_COLS = ["date", "time_ny", "symbol", "signal", "score", "grade", "setup", "price", "entry", "max_price", "stop", "target",
            "rvol", "change_pct", "float_m", "news"]
def log_signal(s, now):
    """Write each new buy/ready signal once to signals_log.csv, so you can review weeks of results later."""
    if DEMO or s["signal"] not in ("BUY NOW", "BUY HALF", "GET READY"):
        return
    k = (now.date(), s["sym"], s["signal"], s["trigger"])
    if k in LOGGED:
        return
    LOGGED.add(k)
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, newline="", encoding="utf-8") as f:
                head = next(csv.reader(f), None)
            if head != LOG_COLS:      # older format: keep it, start a fresh file
                os.replace(LOG_FILE, LOG_FILE.replace(".csv", "_old_%s.csv" % now.strftime("%Y%m%d%H%M%S")))
        new = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_COLS)
            w.writerow([now.date(), now.strftime("%H:%M:%S"), s["sym"], s["signal"], s["score"], s["grade"], s["setup"], s["price"],
                        s["entry"], s["max_price"], s["stop"], s["target"], s["rvol"], s["chg"],
                        None if s["float_m"] is None else round(s["float_m"], 2), s["news"][0]["h"] if s["news"] else ""])
    except Exception:
        log.exception("could not write the signals log")

# ---------------------------------------------------------------- demo data
CYCLE = [0]
def mk_bar(t, o, c, v, r):
    h, l = max(o, c) * (1 + r.uniform(0, .003)), min(o, c) * (1 - r.uniform(0, .003))
    return dict(t=t.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:00Z"), o=o, h=h, l=l, c=c, v=v, vw=(h + l + c) / 3)

def demo_stock(sym, base, seed, chg, rvol, flt, headline, pull, now, fund_extra=None, sec=None):
    r, bars, p = random.Random(seed), [], base
    total = 8 + 22 + max(pull, 1) + (0 if pull else 10)
    t0 = now.replace(second=0, microsecond=0) - timedelta(minutes=total)
    for i in range(8):
        o = p; p = o * (1 + r.uniform(-.003, .004)); bars.append(mk_bar(t0 + timedelta(minutes=i), o, p, r.randint(8, 20) * 1000, r))
    for i in range(22 if pull else 32):
        o = p; p = o * (1 + (0.006 if pull else -0.003) * r.uniform(.5, 1.5)); bars.append(mk_bar(t0 + timedelta(minutes=8 + i), o, p, r.randint(50, 120) * 1000 if pull else r.randint(5, 12) * 1000, r))
    for i in range(pull):
        o = p; p = o * (1 - 0.011 * r.uniform(.6, 1.2)); b = mk_bar(t0 + timedelta(minutes=30 + i), o, p, r.randint(12, 25) * 1000, r); b["h"] = o * 1.001; bars.append(b)
    trig = max(b["h"] for b in bars)
    c = CYCLE[0] % 8
    price = trig * (0.992 + 0.0025 * c) if pull else bars[-1]["c"]
    if pull and price > trig:
        bars.append(mk_bar(t0 + timedelta(minutes=total), bars[-1]["c"], price, r.randint(60, 90) * 1000, r))
    vol = sum(b["v"] for b in bars)
    avg = vol / (rvol * expected_frac(now.hour * 60 + now.minute))
    prev = bars[0]["o"] / (1 + chg / 100)
    daily = [dict(t="", o=prev * .97, h=prev * 1.04, l=prev * .95, c=prev, v=900000) for _ in range(20)]
    news = [dict(h=headline, t=now.isoformat(), url="", src="Benzinga")] if headline else []
    fund = dict(loaded=True, sources=["Demo"], name=sym + " Therapeutics Inc. (fake)", country="United States", sector="Healthcare",
                industry="Biotechnology", employees=42, summary="A fake company used to demonstrate the dashboard.", website="",
                mcap=flt * 1e6 * price * 1.3, hi52=price * 1.6, lo52=price * .4, float=flt * 1e6, float_src="Demo", shares=flt * 1.3e6,
                short_pct=18.0, short_ratio=1.4, inst_pct=6.0, insider_pct=22.0, earnings=None, avg10=avg, sec=sec or [], fv_news=[])
    fund.update(fund_extra or {})
    return dict(sym=sym, price=price, prev_close=prev, bars=bars, avg_vol=avg, hist=None, daily=daily, fund=fund,
                yq=dict(vol=vol * 20, day=now.date().isoformat()), news=news, quote=dict(ap=price * 1.002, bp=price * .998, **{"as": 12, "bs": 20}))

def fetch_demo():
    now = datetime.now(NY); CYCLE[0] += 1
    d = now.date().isoformat()
    return [demo_stock("DEMO1", 3.4, 1, 38, 11, 6.5, "DEMO1 announces FDA fast track designation (synthetic)", 3, now,
                       sec=[dict(form="8-K", date=d, age=0, desc="Current report", url="")]),
            demo_stock("DEMO2", 7.2, 2, 22, 6.2, 14, "DEMO2 wins $40M supply contract (synthetic)", 5, now),
            demo_stock("DEMO3", 2.6, 3, 12, 1.4, 45, None, 0, now, {"country": "China"}),
            demo_stock("DEMO4", 5.1, 4, 31, 8.0, 9, "DEMO4 announces $15M registered direct offering (synthetic)", 3, now,
                       sec=[dict(form="424B5", date=d, age=0, desc="Prospectus", url="")])]

# ---------------------------------------------------------------- market mood
MOOD = {"t": 0, "v": None}
def market_mood():
    if DEMO:
        return dict(tone="good", text="Market is strong (SPY +0.6%, QQQ +0.9%, IWM +1.1%). Breakouts work better on days like this.",
                    items=[["SPY", 0.6, True], ["QQQ", 0.9, True], ["IWM", 1.1, True]])
    if time.time() - MOOD["t"] < 60 and MOOD["v"]:
        return MOOD["v"]
    now = datetime.now(NY)
    sn = alp("/v2/stocks/snapshots", symbols="SPY,QQQ,IWM", feed=FEED)
    sn = sn.get("snapshots", sn)
    items = []
    for s in ("SPY", "QQQ", "IWM"):
        x = sn.get(s) or {}
        try:
            p, prev = x["latestTrade"]["p"], prev_close_of(x, now)
            vw = (x.get("dailyBar") or {}).get("vw")
            items.append([s, round((p / prev - 1) * 100, 2), bool(vw and p >= vw)])
        except (KeyError, TypeError, ZeroDivisionError):
            pass
    if not items:
        return None
    avg = sum(i[1] for i in items) / len(items)
    above = sum(1 for i in items if i[2])
    desc = ", ".join("%s %+.1f%%" % (i[0], i[1]) for i in items)
    if avg >= 0.3 and above >= 2:
        v = dict(tone="good", text="Market is strong (%s). Breakouts work better on days like this." % desc)
    elif avg <= -0.5 or above == 0:
        v = dict(tone="bad", text="Market is weak (%s). Be extra picky, take profits quickly and skip B-grade setups." % desc)
    else:
        v = dict(tone="warn", text="Market is mixed (%s). Trade only A-grade setups." % desc)
    v["items"] = items
    MOOD.update(t=time.time(), v=v)
    return v

# ---------------------------------------------------------------- loop + server
NOTIFIER = Notifier()
ALERTS = AlertManager(NOTIFIER)
RESULT = dict(stocks=[], trades=None, error=None, updated=None, mode="DEMO" if DEMO else "LIVE", win_start=0, win_end=0, window_open=False,
              session="", feed="synthetic" if DEMO else FEED, mood=None, alerts=[], notify={},
              settings=dict(account=C.ACCOUNT_SIZE, risk_pct=C.RISK_PCT, iex=FEED == "iex"))
LOCK = threading.Lock()
HEALTH = dict(last_ok=0.0, started=time.time())
STOP = threading.Event()
_SHUTDOWN = [None]     # set in main(): stops the web server so the process exits

def busy_hours(now):
    """Scan fast from 04:00 to 20:00 New York on weekdays, slowly otherwise (saves API calls overnight)."""
    return DEMO or (now.weekday() < 5 and dtime(4) <= now.time() < dtime(20))

def scan_loop():
    while not STOP.is_set():
        now = datetime.now(NY)
        try:
            ms = market_state(now)
            try:
                mood = market_mood()
            except Exception:
                mood = None
            inputs = fetch_demo() if DEMO else fetch_live()
            if DEMO:
                L2.demo_prices({i["sym"]: i["price"] for i in inputs})
            stocks = [analyze(i, now, ms["window_open"], mood) for i in inputs]
            stocks.sort(key=lambda x: (ORDER[x["signal"]], -x["score"]))
            L2.watch([s["sym"] for s in stocks])     # stream the order books of the best stocks (open trades rank first)
            for s in stocks:
                log_signal(s, now)
            try:
                ALERTS.trades = trade_summary(now)
                ALERTS.on_scan(now, stocks, ms, mood)
            except Exception:
                log.exception("alert error")
            ALERTS.on_error(now, None)
            HEALTH["last_ok"] = time.time()
            pub = {k: v for k, v in ms.items() if k != "today"}
            with LOCK:
                RESULT.update(pub)
                RESULT.update(stocks=stocks[:12], error=None, updated=now.strftime("%H:%M:%S"), mood=mood, trades=trade_summary(now))
        except Exception as e:
            log.exception("scan failed")
            ALERTS.on_error(now, str(e))
            with LOCK:
                RESULT["error"] = str(e)[:300]
        with LOCK:
            RESULT["alerts"] = list(ALERTS.history)[:20]
            RESULT["notify"] = dict(channels=NOTIFIER.names, error=NOTIFIER.last_error, level=C.ALERT_LEVEL)
            RESULT["l2"] = L2.public_status()
        if C.RUN_UNTIL and not DEMO:
            today = market_state(now).get("today")
            if now.time() >= C.RUN_UNTIL or today is None:
                log.info("RUN_UNTIL reached (or market closed today): exiting.")
                STOP.set(); _SHUTDOWN[0] and _SHUTDOWN[0]()
                break
        STOP.wait(INTERVAL if busy_hours(now) else 60)

def load_html():
    try:
        with open(C.WEB_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "<h1>web/index.html is missing</h1>"

class H(BaseHTTPRequestHandler):
    server_version = "MomentumDesk/2.0"
    sys_version = ""

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        if not C.DASHBOARD_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode("utf-8", "ignore").split(":", 1)[-1]
                return hmac.compare_digest(pw.encode(), C.DASHBOARD_PASSWORD.encode())
            except Exception:
                return False
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":       # for uptime monitors and hosting platforms (no password, no data)
            age = time.time() - (HEALTH["last_ok"] or HEALTH["started"])
            ok = age < max(180, INTERVAL * 6) or not busy_hours(datetime.now(NY))
            return self._send(200 if ok else 503, json.dumps(dict(ok=ok, last_scan_seconds_ago=round(age))).encode(), "application/json")
        if not self._authorized():
            return self._send(401, b"Password required", "text/plain", {"WWW-Authenticate": 'Basic realm="Momentum Desk", charset="UTF-8"'})
        if path == "/api/scan":
            with LOCK:
                body = json.dumps(RESULT, default=str).encode()
            return self._send(200, body, "application/json")
        if path == "/api/book":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            sym = (q.get("sym") or [""])[0].upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", sym):
                return self._send(400, b'{"error":"bad symbol"}', "application/json")
            with LOCK:
                s = next((x for x in RESULT["stocks"] if x["sym"] == sym), None)
            ctx = {}
            if s:
                t1 = round(s["entry"] + (s["entry"] - s["stop"]), 2) if s.get("entry") and s.get("stop") else None
                ctx = dict(price=s["price"], entry=s.get("entry"), stop=s.get("stop"), max_price=s.get("max_price"), t1=t1, vwap=s.get("vwap"))
            body = L2.read(sym, ctx) if L2.enabled else dict(ok=False, sym=sym, status=L2.public_status(), why="Level 2 is not set up.")
            return self._send(200, json.dumps(body, default=str).encode(), "application/json")
        if path in ("/", "/index.html"):
            return self._send(200, load_html().encode(), "text/html; charset=utf-8")
        self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        """Only one action: finish the Schwab login from the dashboard (paste the address the browser landed on)."""
        if not self._authorized():
            return self._send(401, b"Password required", "text/plain", {"WWW-Authenticate": 'Basic realm="Momentum Desk", charset="UTF-8"'})
        if self.path.split("?")[0] != "/api/schwab-login":
            return self._send(404, b"Not found", "text/plain")
        # JSON only: browsers can't send it cross-site without permission, so another website can't post here
        n = int(self.headers.get("Content-Length") or 0)
        if not self.headers.get("Content-Type", "").startswith("application/json") or not 0 < n <= 8192:
            return self._send(400, b'{"error":"bad request"}', "application/json")
        try:
            L2.auth.exchange(json.loads(self.rfile.read(n)).get("url", ""))
            out = dict(ok=True)
        except Exception as e:
            out = dict(ok=False, error=str(e)[:300])
        self._send(200, json.dumps(out).encode(), "application/json")

    def log_message(self, *a):
        pass

class Server(ThreadingHTTPServer):
    """Refuse to start a second copy on the same port (Windows otherwise lets two copies share it silently)."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

def startup_check():
    if DEMO:
        return
    if not KEY or not SEC:
        log.error("No Alpaca keys. Put APCA_API_KEY_ID and APCA_API_SECRET_KEY in the .env file (free keys at alpaca.markets).")
        return
    try:
        c = paper("GET", "/clock")
        log.info("Alpaca: connected. Market is %s right now.", "OPEN" if c.get("is_open") else "closed")
    except Exception as e:
        log.warning("Could not connect to Alpaca (%s). Check the key/secret, or create new keys at alpaca.markets.", e)
    try:
        YH.call("/v7/finance/quote", {"symbols": "SPY"})
        log.info("Yahoo Finance: connected (float, short interest, company data).")
    except Exception as e:
        log.warning("Yahoo Finance not reachable (%s). Finviz will be used for float.", str(e)[:80])

def telegram_setup():
    """Prints the chat id(s) that messaged your bot, so you can put TELEGRAM_CHAT_ID in .env."""
    if not C.TELEGRAM_TOKEN:
        sys.exit("Put TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather in Telegram).")
    try:
        r = json.loads(urllib.request.urlopen("https://api.telegram.org/bot%s/getUpdates" % C.TELEGRAM_TOKEN, timeout=15).read())
    except Exception as e:
        sys.exit("Telegram said: %s (is the token right?)" % e)
    chats = {}
    for u in r.get("result", []):
        ch = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if ch.get("id"):
            chats[ch["id"]] = ch.get("title") or ch.get("first_name") or ch.get("username") or ""
    if not chats:
        sys.exit("No messages yet. Open your bot in Telegram, press Start (or send 'hi'), then run this again.")
    for cid, name in chats.items():
        print("  TELEGRAM_CHAT_ID=%s    (%s)" % (cid, name))
    print("\nCopy that line into .env, then run:  python momentum_scanner.py --test-alert")

def test_alert():
    if not NOTIFIER.channels:
        sys.exit("No alert channel set. Fill TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, NTFY_TOPIC or DISCORD_WEBHOOK_URL in .env.")
    res = NOTIFIER.send_now("Test from Momentum Desk",
                            "If you can read this, alerts work.\nExample: GET READY · ABCD $3.40\nEntry $3.42 (never above $3.47) · Stop $3.30 · Target $3.66")
    for name, err in res:
        print("  %-9s %s" % (name, "OK, check your phone" if not err else "FAILED: " + err))
    sys.exit(0 if all(e is None for _, e in res) else 1)

def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # emoji in alert titles vs the Windows console
        except Exception:
            pass
    fmt = dict(level=getattr(logging, C.LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if sys.stderr is None:      # started with pythonw (no console window): log to a file instead
        from logging.handlers import RotatingFileHandler
        fmt["handlers"] = [RotatingFileHandler(os.path.join(C.DATA_DIR, "desk.log"), maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
    logging.basicConfig(**fmt)
    if "--telegram-setup" in sys.argv:
        return telegram_setup()
    if "--test-alert" in sys.argv:
        return test_alert()
    if "--schwab-login" in sys.argv:
        return schwab_login()
    if C.RUN_UNTIL and not DEMO and datetime.now(NY).time() >= C.RUN_UNTIL:
        log.info("Started after RUN_UNTIL %s New York: nothing to do.", C.RUN_UNTIL.strftime("%H:%M"))
        return
    log.info("MOMENTUM DESK starting (%s)", "DEMO - fake data" if DEMO else "LIVE market data, feed=" + FEED)
    load_setup_hist()
    try:
        srv = Server((C.HOST, PORT), H)
    except OSError:
        sys.exit("Port %d is busy: the app is already running somewhere.\nClose the other window first, or just open http://localhost:%d" % (PORT, PORT))
    if C.HOST not in ("127.0.0.1", "localhost") and not C.DASHBOARD_PASSWORD:
        log.warning("The dashboard is reachable from other machines WITHOUT a password. Set DASHBOARD_PASSWORD in .env.")
    log.info("Alerts: %s", ", ".join(NOTIFIER.names) or "none configured (dashboard only). See README.md > Phone alerts.")
    startup_check()
    if DEMO:
        log.info("Level 2: demo order books (fake).")
    elif not L2.auth.configured:
        log.info("Level 2: off. Add SCHWAB_APP_KEY / SCHWAB_APP_SECRET to .env for the real order book (README.md > Level 2).")
    elif not L2.auth.connected:
        log.warning("Level 2: Schwab login needed. Run: python momentum_scanner.py --schwab-login (or use the dashboard).")
    else:
        log.info("Level 2: Schwab connected (login valid %.1f more days).", L2.auth.days_left())
    L2.start(STOP, NOTIFIER)
    threading.Thread(target=scan_loop, daemon=True, name="scan").start()
    if not DEMO:
        threading.Thread(target=enrich_loop, daemon=True, name="enrich").start()
    ALERTS.startup()

    def stop(*_):
        STOP.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()
    _SHUTDOWN[0] = stop
    for sig_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, sig_name):
            signal.signal(getattr(signal, sig_name), stop)

    url = "http://localhost:%d" % PORT
    log.info("Dashboard: %s  (Ctrl+C to stop)", url)
    if not C.HEADLESS:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    for _ in range(40):   # let the last notifications go out (up to 10 s)
        if NOTIFIER.q.empty():
            break
        time.sleep(0.25)
    time.sleep(1)
    log.info("stopped")

if __name__ == "__main__":
    main()
