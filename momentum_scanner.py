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
  * No real Level 2, no halt feed (halts are inferred from missing trades).
"""
import os, re, sys, csv, json, math, time, random, socket, signal, logging, threading, webbrowser, hmac, base64
import urllib.request, urllib.parse, urllib.error, http.cookiejar
from datetime import datetime, timedelta, date, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import config as C
from notifier import Notifier, AlertManager

log = logging.getLogger("desk")
NY = C.NY
KEY, SEC, FEED, WATCH = C.KEY, C.SEC, C.FEED, C.WATCH
WEND = C.TRADE_UNTIL
DEMO, PORT, INTERVAL, LOG_FILE = C.DEMO, C.PORT, C.INTERVAL, C.LOG_FILE
DATA = "https://data.alpaca.markets"
PAPER = "https://paper-api.alpaca.markets/v2"   # used only for the market clock/calendar/asset names; no orders are ever sent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEC_UA = "MomentumDesk/1.0 (personal research tool)"

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
    if run >= 0.03 * sw and 1 <= len(after) <= 6:
        pl = min(b["l"] for b in after)
        depth = (sw - pl) / run
        if depth <= 0.55 and pl <= p <= sw and (sw - p) / sw <= 0.03:
            runv = sum(b["v"] for b in look[max(0, hi - 2):hi + 1]) / min(3, hi + 1)
            pullv = sum(b["v"] for b in after) / len(after)
            return dict(setup="Micro pullback" if len(after) <= 3 else "Bull flag", state="ARMED",
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
ORDER = {"BUY NOW": 0, "BUY SMALL": 1, "GET READY": 2, "WATCH": 3, "TOO LATE": 3, "SKIP": 4}
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

def analyze(inp, now, window_open):
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
        STATE[s] = dict(det, since=time.time())
        state, plan = "ARMED", det
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
    below_vwap = bool(vw and p < vw)
    if below_vwap and state in ("ARMED", "TRIGGERED"):
        blocks.append("below VWAP (sellers in control)")
        sig = "WATCH"
    elif state == "TRIGGERED":
        sig = "BUY NOW" if score >= 85 and not blocks else "BUY SMALL" if score >= 70 and not blocks else "SKIP"
    elif state == "ARMED":
        sig = "SKIP" if [x for x in blocks if "window" not in x] else "GET READY" if score >= 60 else "WATCH"
    elif state == "LATE":
        sig = "TOO LATE"
    else:
        sig = "WATCH"

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
    if sig == "BUY NOW" and room_r is not None and room_r < 0.75:
        sig = "BUY SMALL"            # breaking out straight into resistance: smaller size

    # ---- float rotation (full-market volume from Yahoo / float)
    yq = inp.get("yq") or {}
    fullvol = yq.get("vol") if yq.get("day") == now.date().isoformat() else None
    rotation = round(fullvol / fund["float"], 2) if fullvol and fund.get("float") else None

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
    rr = round((target - entry) / (entry - stop), 1) if plan and target else None

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
    real_blocks = [b for b in blocks if "window" not in b]
    if sig == "SKIP" and real_blocks:
        why = "avoid: " + real_blocks[0]

    tail = bars[-60:]
    return dict(sym=s, price=round(p, 2), chg=round(chg, 1), rvol=round(rvol, 1), float_m=fl, spread=None if spread is None else round(spread, 2),
                spread_iex=iex, vwap=vw and round(vw, 2), ema9=e9 and round(e9, 2), score=score, grade=grade, signal=sig, state=state,
                setup=plan["setup"] if plan else None, note=plan.get("note") if plan else None,
                trigger=plan and round(plan["trigger"], 2), entry=entry, stop=stop, target=target, max_price=max_price, blocks=blocks, rr=rr,
                halted=halted, idle_min=None if idle is None else round(idle, 1), room_r=room_r, res_above=res_above,
                hod=round(hod, 2), tips=T, why=why, pillars=pillars, levels=levels, next_res=next_res, next_sup=next_sup,
                rotation=rotation, fullvol=fullvol, sec=sec[:12], sec_flags=sflags,
                fund={k: fund.get(k) for k in ("loaded", "sources", "name", "country", "sector", "industry", "employees", "summary", "website",
                                               "mcap", "hi52", "lo52", "float", "float_src", "shares", "short_pct", "short_ratio",
                                               "inst_pct", "insider_pct", "earnings", "avg10")},
                rules=R, news=news_all, news_kind=kind,
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
    syms = set(WATCH)
    mv = alp("/v1beta1/screener/stocks/movers", top=50)
    for g in mv.get("gainers", []):
        if 1.5 <= g.get("price", 0) <= 25 and g.get("percent_change", 0) >= 8:
            syms.add(g["symbol"])
    try:
        for a in alp("/v1beta1/screener/stocks/most-actives", by="volume", top=50).get("most_actives", []):
            syms.add(a["symbol"])
    except Exception:
        pass
    syms = {s for s in syms if "." not in s or s in WATCH}
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
        if 1.5 <= p <= 25 and (chg >= 8 or s in WATCH):
            cand.append((chg, s, sn, prev))
    keep = []
    for c in sorted(cand, key=lambda c: -c[0]):
        if c[1] in WATCH or not FUND_RX.search(asset_name(c[1])):
            keep.append(c)
        if len(keep) >= 15:
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
    if DEMO or s["signal"] not in ("BUY NOW", "BUY SMALL", "GET READY"):
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
RESULT = dict(stocks=[], error=None, updated=None, mode="DEMO" if DEMO else "LIVE", win_start=0, win_end=0, window_open=False,
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
            stocks = [analyze(i, now, ms["window_open"]) for i in (fetch_demo() if DEMO else fetch_live())]
            stocks.sort(key=lambda x: (ORDER[x["signal"]], -x["score"]))
            for s in stocks:
                log_signal(s, now)
            try:
                ALERTS.on_scan(now, stocks, ms, mood)
            except Exception:
                log.exception("alert error")
            ALERTS.on_error(now, None)
            HEALTH["last_ok"] = time.time()
            pub = {k: v for k, v in ms.items() if k != "today"}
            with LOCK:
                RESULT.update(pub)
                RESULT.update(stocks=stocks[:12], error=None, updated=now.strftime("%H:%M:%S"), mood=mood)
        except Exception as e:
            log.exception("scan failed")
            ALERTS.on_error(now, str(e))
            with LOCK:
                RESULT["error"] = str(e)[:300]
        with LOCK:
            RESULT["alerts"] = list(ALERTS.history)[:20]
            RESULT["notify"] = dict(channels=NOTIFIER.names, error=NOTIFIER.last_error, level=C.ALERT_LEVEL)
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
        if path in ("/", "/index.html"):
            return self._send(200, load_html().encode(), "text/html; charset=utf-8")
        self._send(404, b"Not found", "text/plain")

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
    log.info("MOMENTUM DESK starting (%s)", "DEMO - fake data" if DEMO else "LIVE market data, feed=" + FEED)
    try:
        srv = Server((C.HOST, PORT), H)
    except OSError:
        sys.exit("Port %d is busy: the app is already running somewhere.\nClose the other window first, or just open http://localhost:%d" % (PORT, PORT))
    if C.HOST not in ("127.0.0.1", "localhost") and not C.DASHBOARD_PASSWORD:
        log.warning("The dashboard is reachable from other machines WITHOUT a password. Set DASHBOARD_PASSWORD in .env.")
    log.info("Alerts: %s", ", ".join(NOTIFIER.names) or "none configured (dashboard only). See README.md > Phone alerts.")
    startup_check()
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
