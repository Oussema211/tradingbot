"""
Yahoo Finance 1-minute candles with FULL-MARKET volume (free, no key).

Why: the free Alpaca feed is IEX only (about 2-3% of all trading). On a small cap that means gappy candles and a volume
number that is a tiny sample (BNAI on 2026-11-23, 9:30-11:00: IEX 28,331 shares vs Yahoo 15,100,783). Volume checks,
VWAP and the pullback patterns are all much more accurate on the full tape.

Limits (honest):
  * Unofficial endpoint: Yahoo can rate-limit, block cloud servers or change it. A circuit breaker turns it off for
    10 minutes after repeated failures, and the scanner falls back to Alpaca IEX automatically, per stock.
  * Candles can lag the real tape by a few seconds.
"""
import json, time, threading, logging, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

log = logging.getLogger("desk")
HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
UTC = timezone.utc


def _iso(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_bars(res):
    """Yahoo chart result -> Alpaca-style bars: {t, o, h, l, c, v, vw}. Rows with no trades (halts, gaps) are skipped."""
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c, v = (q.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    out = []
    for i, t in enumerate(ts):
        try:
            if c[i] is None or o[i] is None or h[i] is None or l[i] is None:
                continue
            vol = int(v[i] or 0)
        except IndexError:
            break
        out.append(dict(t=_iso(t), o=o[i], h=h[i], l=l[i], c=c[i], v=vol, vw=(h[i] + l[i] + c[i]) / 3))
    return out


class YahooFeed:
    """Thread-safe Yahoo chart client with a per-symbol cache, a request budget and a circuit breaker."""

    def __init__(self, ua, ny, enabled=True, workers=5):
        self.ua, self.ny, self.enabled = ua, ny, enabled
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="yahoo")
        self.lock = threading.Lock()
        self.fails, self.off_until, self.host = 0, 0.0, 0
        self.last_error, self.ok_total, self.err_total = None, 0, 0
        self.today = {}      # sym -> dict(bars, fetched)
        self.hist = {}       # sym -> dict(day, curves, avg_vol, daily)

    # ---------------------------------------------------------------- raw calls
    def available(self):
        return self.enabled and time.time() >= self.off_until

    def _chart(self, sym, rng, interval, prepost):
        params = dict(range=rng, interval=interval, includePrePost="true" if prepost else "false")
        url = "%s/v8/finance/chart/%s?%s" % (HOSTS[self.host], urllib.parse.quote(sym), urllib.parse.urlencode(params))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": self.ua}), timeout=10) as r:
                j = json.loads(r.read())
            res = ((j.get("chart") or {}).get("result") or [None])[0]
            if not res:
                raise LookupError("no data for %s" % sym)
            with self.lock:
                self.fails, self.ok_total = 0, self.ok_total + 1
            return res
        except Exception as e:
            unknown = isinstance(e, LookupError) or (isinstance(e, urllib.error.HTTPError) and e.code == 404)
            with self.lock:
                self.err_total += 1
                self.last_error = "%s: %s" % (sym, e)
                if not unknown:                        # blocked, rate-limited, timeout: counts toward the breaker
                    self.fails += 1
                    self.host = 1 - self.host          # try the other Yahoo host next time
                if self.fails >= 4 and time.time() >= self.off_until:
                    self.off_until = time.time() + 600
                    log.warning("Yahoo candles paused for 10 minutes (%s). Using Alpaca IEX meanwhile.", self.last_error)
            raise

    def _today(self, sym):
        res = self._chart(sym, "1d", "1m", True)
        d0 = datetime.now(self.ny).date()
        bars = [b for b in to_bars(res) if datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(self.ny).date() == d0]
        with self.lock:
            self.today[sym] = dict(bars=bars, fetched=time.time())

    def _history(self, sym):
        """Same-time-of-day volume curves (last 4-5 sessions) + 20-day average volume + 30 daily bars, once per day."""
        d0 = datetime.now(self.ny).date()
        m1 = to_bars(self._chart(sym, "5d", "1m", True))
        days = {}
        for b in m1:
            t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(self.ny)
            if t.date() < d0:
                days.setdefault(t.date(), [0] * 1440)[t.hour * 60 + t.minute] += b["v"]
        curves = []
        for k in sorted(days):
            acc, cum = 0, []
            for v in days[k]:
                acc += v
                cum.append(acc)
            curves.append(cum)
        dl = [b for b in to_bars(self._chart(sym, "3mo", "1d", False))
              if datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(self.ny).date() < d0]
        vols = [b["v"] for b in dl][-20:]
        with self.lock:
            self.hist[sym] = dict(day=d0, curves=curves[-10:], avg_vol=sum(vols) / len(vols) if vols else 0, daily=dl[-30:])

    # ---------------------------------------------------------------- scheduling
    def refresh(self, syms, hot=(), max_calls=12, fast=10, slow=30, wait=6.0):
        """Refresh as many stocks as the budget allows, in parallel. 'hot' stocks (loaded setups, open trades)
        refresh every `fast` seconds, the others every `slow` seconds. New stocks also get their history once."""
        if not self.available() or not syms:
            return
        now, d0 = time.time(), datetime.now(self.ny).date()
        hot = set(hot)
        need_hist = [s for s in syms if (self.hist.get(s) or {}).get("day") != d0][:3]
        def age(s):
            return now - (self.today.get(s) or {}).get("fetched", 0)
        due = [s for s in syms if age(s) >= (fast if s in hot else slow)]
        due.sort(key=lambda s: (s not in hot, -age(s)))
        jobs = [(self._history, s) for s in need_hist] + [(self._today, s) for s in due]
        jobs = jobs[:max_calls]
        futs = [self.pool.submit(fn, s) for fn, s in jobs]
        end = time.time() + wait
        for f in futs:
            try:
                f.result(timeout=max(0.1, end - time.time()))
            except Exception:
                pass        # recorded in _chart; the stock simply stays on IEX this scan

    def data(self, sym, max_age=120):
        """Fresh Yahoo data for one stock, or None (then the caller uses IEX). Candles, history and average volume
        all come from Yahoo together, so relative volume compares like with like."""
        if not self.enabled:
            return None
        d0 = datetime.now(self.ny).date()
        with self.lock:
            t, h = self.today.get(sym), self.hist.get(sym)
        if not t or not h or h["day"] != d0 or time.time() - t["fetched"] > max_age or len(t["bars"]) < 2:
            return None
        return dict(bars=t["bars"], hist=h["curves"], avg_vol=h["avg_vol"], daily=h["daily"])

    def status(self):
        with self.lock:
            return dict(enabled=self.enabled, ok=self.available(), paused_for=max(0, round(self.off_until - time.time())),
                        calls_ok=self.ok_total, calls_failed=self.err_total, last_error=self.last_error)
