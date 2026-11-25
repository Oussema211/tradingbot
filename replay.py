#!/usr/bin/env python3
"""
REPLAY: run a past trading day through the scanner's REAL rules, minute by minute, and see what it would have done.

    python replay.py                              today's top movers (after the window), full-market data
    python replay.py --date 2026-11-20 --symbols BNAI,OTLY,MEDS
    python replay.py --feed iex                   the same, on the free IEX sample (what the bot saw before Yahoo)
    python replay.py --verbose                    print every signal change, not just the summary
    python replay.py --days 40 --save-stats       BACKTEST: the last 40 trading days, each day's top gainers found
                                                  automatically; saves win rate / avg R per setup and grade to
                                                  setup_stats.json, which the live bot shows on every alert (and
                                                  uses to mute setups that lose money)

Data: Alpaca's full-market SIP history (free on the Basic plan for anything older than 15 minutes), the same 1-minute
candles your own chart shows. Nothing here can touch your live bot: it writes only to a temporary folder
(except --save-stats, which writes setup_stats.json on purpose).

Honest limits:
  * The live bot scans every 3 seconds; the replay walks each candle's likely path in --scan second steps (the real
    tick order inside a minute is unknown). Fills are assumed at the bot's own limit rules.
  * Float, short interest and SEC filings are today's values (the free sources have no history).
  * Without --symbols on a past date there is no free "top gainers of that day" list, so name the stocks
    (--days builds one from daily candles: stocks that ran 15%+ from the prior close on 1M+ shares).
  * Backtest stock picks use each day's full daily candle (the live bot only sees a stock once it moves).
    Treat the stats as a strong guide, not a promise.
"""
import os, sys, csv, json, time, bisect, argparse, tempfile, logging, subprocess
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timedelta, time as dtime

ap = argparse.ArgumentParser(description="Replay a trading day through the scanner rules")
ap.add_argument("--date", help="YYYY-MM-DD (New York). Default: today")
ap.add_argument("--symbols", help="comma separated. Default: today's top gainers (today only)")
ap.add_argument("--feed", default="sip", choices=["sip", "iex"], help="sip = full market (default), iex = free sample")
ap.add_argument("--verbose", action="store_true")
ap.add_argument("--scan", type=int, default=3, help="seconds between the live bot's scans (default 3, as live)")
ap.add_argument("--csv", help="also write every signal change to this CSV file")
ap.add_argument("--days", type=int, help="backtest the last N trading days (stocks found automatically)")
ap.add_argument("--per-day", type=int, default=10, help="with --days: top gainers replayed per day (default 10)")
ap.add_argument("--save-stats", action="store_true", help="with --days: write setup_stats.json for the live bot")
ap.add_argument("--workers", type=int, default=4, help="with --days: days replayed in parallel (default 4)")
ap.add_argument("--json-out", help=argparse.SUPPRESS)     # internal: a backtest worker writes its day's trades here
args = ap.parse_args()

# isolate: the replay must never write into the live bot's files (only --save-stats writes setup_stats.json there)
REAL_DATA = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="desk-replay-")
os.environ["YAHOO_CANDLES"] = "0"
sys.argv = [sys.argv[0]]
import config as C
import momentum_scanner as M

logging.basicConfig(level=logging.WARNING, format="%(message)s")
NY = C.NY


class SimClock:
    """analyze() stores 'when was this pattern first seen' with time.time(): feed it the replay clock instead."""
    def __init__(self, real):
        self.real, self.now = real, real.time()
    def time(self):
        return self.now
    def __getattr__(self, k):
        return getattr(self.real, k)


CLOCK = SimClock(M.time)
M.time = CLOCK
M.FEED = "iex"         # keeps the spread check neutral (the replay has no historical quotes)
C.MUTE_LOSING_SETUPS = False       # the replay measures every setup, so it must not mute any
M.load_setup_hist = lambda: None   # and every day is judged on the same rules (no "setup has been losing" drift)

_alp = M.alp
def alp_retry(path, **params):
    """Alpaca allows ~200 calls a minute: wait and retry when a long backtest hits the limit."""
    for k in range(6):
        try:
            return _alp(path, **params)
        except RuntimeError as e:
            if "429" not in str(e) or k == 5:
                raise
            time.sleep(5 * (k + 1))
M.alp = alp_retry


def ts(b):
    return M.parse_t(b["t"]).astimezone(NY)


def bars(syms, tf, start, end, feed):
    res, tok = {s: [] for s in syms}, None
    for _ in range(200):
        p = dict(symbols=",".join(syms), timeframe=tf, start=start.isoformat(), end=end.isoformat(), limit=10000,
                 feed=feed, adjustment="raw", sort="asc")
        if tok:
            p["page_token"] = tok
        r = M.alp("/v2/stocks/bars", **p)
        for s, b in (r.get("bars") or {}).items():
            res.setdefault(s, []).extend(b)
        tok = r.get("next_page_token")
        if not tok:
            break
    return res


def news_for(syms, start, end):
    out, tok = {}, None
    for _ in range(20):
        p = dict(symbols=",".join(syms), start=start.isoformat(), end=end.isoformat(), limit=50, sort="desc")
        if tok:
            p["page_token"] = tok
        r = M.alp("/v1beta1/news", **p)
        for n in r.get("news", []):
            for s in n.get("symbols", []):
                out.setdefault(s, []).append(dict(h=n.get("headline", ""), t=n.get("created_at", ""), url=n.get("url", ""), src="Benzinga"))
        tok = r.get("next_page_token")
        if not tok:
            break
    return out


def sample_path(b, n=6):
    """About n prices along the candle's likely path (green: open-low-high-close, red: open-high-low-close), like the
    live bot's scans every 10 seconds. Each leg gets steps in proportion to its length and ends exactly on its vertex,
    so the low, the high and the close are always visited (a stop or target touched inside the minute counts)."""
    pts = [b["o"], b["l"], b["h"], b["c"]] if b["c"] >= b["o"] else [b["o"], b["h"], b["l"], b["c"]]
    segs = [abs(y - x) for x, y in zip(pts, pts[1:])]
    total = sum(segs)
    if not total:
        return [b["c"]]
    out = []
    for (x, y), seg in zip(zip(pts, pts[1:]), segs):
        if not seg:
            continue
        m = max(1, round(n * seg / total))
        out += [x + (y - x) * j / m for j in range(1, m + 1)]
    return out


def curves(minute_bars, day):
    days = {}
    for b in minute_bars:
        t = ts(b)
        if t.date() < day:
            days.setdefault(t.date(), [0] * 1440)[t.hour * 60 + t.minute] += b["v"]
    out = []
    for k in sorted(days):
        acc, cum = 0, []
        for v in days[k]:
            acc += v
            cum.append(acc)
        out.append(cum)
    return out[-10:]


def replay_day(day, syms, feed, quiet=False, now_real=None):
    """Run one day through the real rules. Returns (trades, reasons, best, pil, log_rows)."""
    now_real = now_real or datetime.now(NY)
    M.STATE.clear()
    d0 = datetime.combine(day, dtime(4), NY)
    d_end = min(datetime.combine(day, dtime(16)), now_real.replace(tzinfo=None) - timedelta(minutes=16)).replace(tzinfo=NY)
    if d_end <= d0:
        raise RuntimeError("That day has no data yet (SIP history is free only after 15 minutes).")
    if not quiet:
        print("Replaying %s on %s data: %s" % (day, feed.upper(), ", ".join(syms)))

    allsyms = syms + ["SPY", "QQQ", "IWM"]
    today = bars(allsyms, "1Min", d0, d_end, feed)
    hist = bars(syms, "1Min", d0 - timedelta(days=16), d0, feed)
    daily = bars(allsyms, "1Day", d0 - timedelta(days=60), d0, feed)
    daily = {k: [b for b in v if ts(b).date() < day] for k, v in daily.items()}   # today's daily candle is stamped 00:00
    news = news_for(syms, d0 - timedelta(hours=48), d_end)

    for s in syms:                      # float, short interest, SEC filings (today's values, cached across days)
        if s in M.ENRICH:
            continue
        try:
            M.ENRICH[s] = M.enrich(s)
        except Exception:
            pass

    prev_close = {s: (daily.get(s) or [{}])[-1].get("c") for s in allsyms}
    avg_vol = {}
    for s in syms:
        v = [b["v"] for b in daily.get(s, [])][-20:]
        avg_vol[s] = sum(v) / len(v) if v else 0
    hc = {s: curves(hist.get(s, []), day) for s in syms}
    tt = {s: [ts(b) for b in today.get(s, [])] for s in allsyms}       # parsed once (bisect below)
    rth = {s: bisect.bisect_left(tt[s], datetime.combine(day, dtime(9, 30), NY)) for s in allsyms}

    def upto(s, t):
        return today.get(s, [])[:bisect.bisect_left(tt[s], t)]

    def mood_at(t):
        items = []
        for s in ("SPY", "QQQ", "IWM"):
            bl = upto(s, t)[rth[s]:]
            if not bl or not prev_close.get(s):
                continue
            pv = sum(b["vw"] * b["v"] for b in bl); vv = sum(b["v"] for b in bl)
            p = bl[-1]["c"]
            items.append([s, round((p / prev_close[s] - 1) * 100, 2), bool(vv and p >= pv / vv)])
        if not items:
            return None
        avg = sum(i[1] for i in items) / len(items); above = sum(1 for i in items if i[2])
        tone = "good" if avg >= 0.3 and above >= 2 else "bad" if avg <= -0.5 or above == 0 else "warn"
        return dict(tone=tone, text="", items=items)

    log_rows, last_sig, grades = [], {}, {}
    reasons = {s: Counter() for s in syms}
    best = {s: (None, -1) for s in syms}      # closest it got: (signal, checks passed)
    pil = {s: None for s in syms}             # best pillar grade seen in the window
    bs = datetime.combine(day, dtime(9, 30), NY)       # start of the candle being "lived through"
    while bs + timedelta(seconds=59) <= d_end:
        mood = mood_at(bs)
        active = None
        if bs.time() >= C.TRADE_UNTIL:          # after the window only open trades still need managing
            active = {x["sym"] for x in M.TRADES["list"] if not x.get("closed")} if M.TRADES["day"] == day.isoformat() else set()
            if not active:
                break
        for s in syms:
            if not prev_close.get(s) or (active is not None and s not in active):
                continue
            base = upto(s, bs)                           # candles finished before this minute
            i = len(base)
            cur = today[s][i] if i < len(today.get(s, [])) and tt[s][i] == bs else None
            # the live bot scans every few seconds: walk the candle's likely path (green: open-low-high-close, red: open-high-low-close)
            path = sample_path(cur, max(1, 60 // args.scan)) if cur else [None]
            r = None
            for k, px in enumerate(path):
                t = bs + timedelta(seconds=59 if px is None else int(60 * (k + 1) / len(path)) - 1)
                CLOCK.now = t.timestamp()
                if px is None:
                    bl = base
                else:
                    seen = path[:k + 1]
                    part = dict(cur, h=max(seen), l=min(seen), c=px, v=int(cur["v"] * (k + 1) / len(path)))
                    bl = base + [part]
                if not bl:
                    continue
                win = C.TRADE_FROM <= t.time() < C.TRADE_UNTIL
                inp = dict(sym=s, price=bl[-1]["c"], prev_close=prev_close[s], bars=bl, avg_vol=avg_vol[s], hist=hc[s],
                           daily=daily.get(s, [])[-30:], fund=M.fundamentals(s), yq=dict(vol=sum(b["v"] for b in bl), day=day.isoformat()),
                           news=[n for n in news.get(s, []) if n["t"] and M.parse_t(n["t"]) <= t],
                           quote=dict(ap=None, bp=None, **{"as": None, "bs": None}), last_t=bl[-1]["t"], src=feed)
                r = M.analyze(inp, t, win, mood)
                sig = r["signal"]
                if r.get("trade") and r["trade"]["id"] not in grades:
                    grades[r["trade"]["id"]] = r["pillar_grade"]
                if win and (pil[s] is None or r["pillar_score"] > pil[s][0]):
                    pil[s] = (r["pillar_score"], r["pillar_grade"], r["pillar_label"])
                if win and (sig in ("GET READY", "BUY NOW", "BUY HALF") or r["passed"] > best[s][1]) and best[s][0] not in ("BUY NOW", "BUY HALF"):
                    best[s] = (sig, max(r["passed"], best[s][1]))
                if sig != last_sig.get(s):
                    last_sig[s] = sig
                    row = [t.strftime("%H:%M:%S"), s, sig, r["price"], r.get("setup") or "", r.get("entry") or "", r.get("stop") or "",
                           r.get("target") or "", r["passed"], r["total"], r["reason"] or ""]
                    log_rows.append(row)
                    if not quiet and (args.verbose or sig in ("GET READY", "BUY NOW", "BUY HALF", "SELL HALF", "EXIT NOW")):
                        print("  %s %-5s %-10s $%-7s %s" % (row[0][:5], s, sig, row[3],
                              ("%s entry %s stop %s target %s" % (row[4], row[5], row[6], row[7])) if sig in ("GET READY", "BUY NOW", "BUY HALF")
                              else row[10]))
            if r and C.TRADE_FROM <= bs.time() < C.TRADE_UNTIL:
                reasons[s][r["reason"] or r["signal"]] += 1
        bs += timedelta(minutes=1)

    trades = [dict(x, grade=grades.get(x["id"], "?"), day=day.isoformat())
              for x in (M.TRADES["list"] if M.TRADES["day"] == day.isoformat() else [])]
    return trades, reasons, best, pil, log_rows


def print_day(syms, trades, reasons, best, pil, log_rows):
    print("\n=== TRADES THE BOT WOULD HAVE TAKEN ===")
    if not trades:
        print("  none")
    for tr in trades:
        print("  %-5s %s  in $%.2f  stop $%.2f  target $%.2f  ->  %s at %s $%s  %+.2fR  (%s)  grade %s" % (
            tr["sym"], tr["opened_ny"], tr["entry"], tr["stop0"], tr["target"], tr["result"] or "OPEN",
            tr["closed_ny"] or "-", tr["exit"] or "-", tr["r"] or 0, tr["why"] or "still open", tr["grade"]))
    done = [x for x in trades if x.get("closed")]
    if done:
        print("  total %+.2fR over %d trades (%d wins)" % (sum(x["r"] for x in done), len(done), sum(1 for x in done if x["result"] == "WIN")))

    print("\n=== WHY EACH STOCK DID OR DIDN'T TRADE (minutes in the window) ===")
    for s in syms:
        top = reasons[s].most_common(3)
        if top:
            print("  %-5s best: %-10s %-12s | %s" % (s, best[s][0], ("pillars %s %d" % (pil[s][1], pil[s][0])) if pil[s] else "", " · ".join("%s (%d)" % (k[:55], n) for k, n in top)))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time_ny", "symbol", "signal", "price", "setup", "entry", "stop", "target", "passed", "total", "reason"])
            w.writerows(log_rows)
        print("\nSignal log written to", args.csv)


def find_gainers(n_days, per_day, now_real):
    """Each past day's top gainers from daily candles of every active US stock (Alpaca has no historical movers list)."""
    assets = M.paper("GET", "/assets?status=active&asset_class=us_equity")
    names = {a["symbol"]: a.get("name") or "" for a in assets
             if a.get("tradable") and a.get("exchange") in ("NASDAQ", "NYSE", "AMEX") and "." not in a["symbol"] and "/" not in a["symbol"]}
    syms = sorted(s for s in names if not M.FUND_RX.search(names[s]))
    end = datetime.combine(now_real.date(), dtime(0), NY)
    start = end - timedelta(days=int(n_days * 1.6) + 10)
    print("Finding each day's top gainers among %d stocks..." % len(syms))
    daily = {}
    for i in range(0, len(syms), 400):
        daily.update(bars(syms[i:i + 400], "1Day", start, end, "sip"))
    spy = bars(["SPY"], "1Day", start, end, "sip").get("SPY", [])
    days = [ts(b).date() for b in spy if ts(b).date() < now_real.date()][-n_days:]
    picks = {d: [] for d in days}
    for s, bl in daily.items():
        for prev, b in zip(bl, bl[1:]):
            d = ts(b).date()
            if d not in picks or not prev["c"]:
                continue
            run = b["h"] / prev["c"] - 1
            if run >= 0.15 and 1.5 <= b["o"] <= 25 and b["v"] >= 1_000_000:
                picks[d].append((run, s))
    return [(d, [s for _, s in sorted(picks[d], reverse=True)[:per_day]]) for d in days]


def stats_of(trades):
    def agg(rows):
        n = len(rows); w = sum(1 for x in rows if x["result"] == "WIN"); r = round(sum(x["r"] for x in rows), 2)
        return dict(n=n, w=w, r=r, avg=round(r / n, 3) if n else 0)
    by_setup, by_grade = {}, {}
    for x in trades:
        by_setup.setdefault(x["setup"], []).append(x)
        by_grade.setdefault("%s|%s" % (x["setup"], (x["grade"] or "?")[0]), []).append(x)
    return {k: agg(v) for k, v in by_setup.items()}, {k: agg(v) for k, v in by_grade.items()}


def backtest():
    now_real = datetime.now(NY)
    plan = find_gainers(args.days, args.per_day, now_real)
    if not plan:
        sys.exit("No trading days found.")
    all_trades = []
    print("Backtesting %d days (%s to %s), up to %d stocks a day, %d at a time.\n" % (
        len(plan), plan[0][0], plan[-1][0], args.per_day, args.workers))
    tmp = os.environ["DATA_DIR"]
    env = dict(os.environ, DATA_DIR=REAL_DATA, PYTHONIOENCODING="utf-8")

    def run(item):
        """Each day in its own process: the scanner keeps per-day state in module globals."""
        d, syms = item
        if not syms:
            return d, syms, None, "no gainers"
        out = os.path.join(tmp, "day-%s.json" % d)
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--date", d.isoformat(), "--symbols", ",".join(syms),
                            "--json-out", out], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode or not os.path.exists(out):
            return d, syms, None, (p.stderr.strip().splitlines() or ["failed"])[-1][:120]
        with open(out, encoding="utf-8") as f:
            return d, syms, json.load(f), None

    with ThreadPoolExecutor(max(1, args.workers)) as ex:
        for d, syms, tr, err in ex.map(run, plan):       # map keeps the days in order
            if err:
                print("  %s  %s" % (d, err if err == "no gainers" else "skipped: " + err), flush=True)
                continue
            done = [x for x in tr if x.get("closed")]
            all_trades += done
            print("  %s  %2d stocks  %d trades  %+.2fR  %s" % (d, len(syms), len(done), sum(x["r"] for x in done),
                  " ".join("%s(%s %+.1f)" % (x["sym"], x["grade"], x["r"]) for x in done)), flush=True)
    by_setup, by_grade = stats_of(all_trades)
    n = len(all_trades); w = sum(1 for x in all_trades if x["result"] == "WIN"); r = sum(x["r"] for x in all_trades)
    print("\n=== BACKTEST RESULT ===")
    print("  %d trades · %d%% win · %+.2fR total · %+.3fR per trade" % (n, round(100 * w / n) if n else 0, r, r / n if n else 0))
    print("\n  %-34s %6s %6s %9s %9s" % ("setup (· grade)", "trades", "win%", "total R", "avg R"))
    rows = sorted(by_setup.items(), key=lambda kv: -kv[1]["n"]) + [(k.replace("|", " · "), v) for k, v in sorted(by_grade.items())]
    for k, v in rows:
        flag = "" if v["n"] < C.STATS_MIN_TRADES else "  <- muted live" if v["avg"] <= 0 else "  <- kept"
        print("  %-34s %6d %5d%% %+9.2f %+9.3f%s" % (k[:34], v["n"], round(100 * v["w"] / v["n"]), v["r"], v["avg"], flag))
    print("  (fewer than %d trades: shown on alerts as a small sample, never muted)" % C.STATS_MIN_TRADES)
    if args.save_stats:
        out = {"generated": now_real.isoformat(timespec="seconds"), "days": len(plan), "from": plan[0][0].isoformat(),
               "to": plan[-1][0].isoformat(), "trades_n": n, "per_day": args.per_day, "must_pillars": C.MUST_PILLARS,
               "by_setup": by_setup, "by_grade": by_grade,
               "trades": [{k: x.get(k) for k in ("day", "sym", "setup", "grade", "opened_ny", "entry", "stop0", "exit", "result", "r", "why")}
                          for x in all_trades]}
        path = os.path.join(REAL_DATA, "setup_stats.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        os.replace(path + ".tmp", path)
        print("\nSaved %s: the live bot picks it up within seconds (no restart needed)." % path)


def main():
    if args.days:
        return backtest()
    now_real = datetime.now(NY)
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else now_real.date()
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif day == now_real.date():
        mv = M.alp("/v1beta1/screener/stocks/movers", top=50)
        syms = [g["symbol"] for g in mv.get("gainers", []) if 1.5 <= g.get("price", 0) <= 25 and g.get("percent_change", 0) >= 8]
        syms = [s for s in syms if "." not in s and not M.FUND_RX.search(M.asset_name(s))][:20]
    else:
        sys.exit("For a past date, name the stocks: --symbols BNAI,OTLY,...  (or backtest many days: --days 40)")
    if not syms:
        sys.exit("No stocks to replay.")
    try:
        res = replay_day(day, syms, args.feed, quiet=bool(args.json_out), now_real=now_real)
    except RuntimeError as e:
        sys.exit(str(e))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(res[0], f)
        return
    print_day(syms, *res)


if __name__ == "__main__":
    main()
