"""
Phone / desktop notifications for Momentum Desk.

Channels (use any, or several at once; set them in .env):
  Telegram  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   (recommended: free, private, instant)
  ntfy      NTFY_TOPIC                              (no account: install the ntfy app, subscribe to the topic)
  Discord   DISCORD_WEBHOOK_URL

Sending happens on a background thread with retries, so a slow channel never delays the scanner.
"""
import json, time, queue, logging, threading, html, os
import urllib.request, urllib.error
from collections import deque
from datetime import datetime, timedelta

import config as C

log = logging.getLogger("desk.alerts")
UA = "MomentumDesk/2.0"


def _post_json(url, payload, headers=None, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip().startswith(b"{") else {}


# ---------------------------------------------------------------- channels
class Telegram:
    name = "Telegram"

    def __init__(self, token, chat):
        self.token, self.chat = token, chat

    def send(self, m):
        text = "<b>%s</b>\n%s" % (html.escape(m["title"]), html.escape(m["body"]))
        if m.get("url"):
            text += '\n<a href="%s">Open the dashboard</a>' % html.escape(m["url"], quote=True)
        _post_json("https://api.telegram.org/bot%s/sendMessage" % self.token,
                   {"chat_id": self.chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True,
                    "disable_notification": m["priority"] <= 2})


class Ntfy:
    name = "ntfy"

    def __init__(self, server, topic, token):
        self.server, self.topic, self.token = server, topic, token

    def send(self, m):
        p = {"topic": self.topic, "title": m["title"], "message": m["body"], "priority": m["priority"], "tags": m.get("tags") or []}
        if m.get("url"):
            p["click"] = m["url"]
        _post_json(self.server, p, {"Authorization": "Bearer " + self.token} if self.token else None)


class Discord:
    name = "Discord"

    def __init__(self, url):
        self.url = url

    def send(self, m):
        body = "**%s**\n%s" % (m["title"], m["body"])
        if m.get("url"):
            body += "\n<%s>" % m["url"]
        _post_json(self.url, {"content": body[:1900]})


class Notifier:
    def __init__(self):
        self.channels = []
        if C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT:
            self.channels.append(Telegram(C.TELEGRAM_TOKEN, C.TELEGRAM_CHAT))
        if C.NTFY_TOPIC:
            self.channels.append(Ntfy(C.NTFY_SERVER, C.NTFY_TOPIC, C.NTFY_TOKEN))
        if C.DISCORD_WEBHOOK:
            self.channels.append(Discord(C.DISCORD_WEBHOOK))
        self.q = queue.Queue(maxsize=200)
        self.last_error = None
        threading.Thread(target=self._worker, daemon=True, name="notifier").start()

    @property
    def names(self):
        return [c.name for c in self.channels]

    def send(self, title, body, priority=3, tags=None, url=None):
        """priority: 1 min .. 5 urgent (ntfy scale; mapped for other channels)."""
        m = dict(title=title, body=body, priority=priority, tags=tags or [], url=url or (C.PUBLIC_URL or None))
        log.info("ALERT %s | %s", title, body.replace("\n", " | "))
        if not self.channels:
            return
        try:
            self.q.put_nowait(m)
        except queue.Full:
            log.warning("notification queue full, dropping: %s", title)

    def send_now(self, title, body):
        """Synchronous send used by --test-alert. Returns a list of (channel, error-or-None)."""
        m = dict(title=title, body=body, priority=3, tags=["white_check_mark"], url=C.PUBLIC_URL or None)
        out = []
        for ch in self.channels:
            try:
                ch.send(m); out.append((ch.name, None))
            except Exception as e:
                out.append((ch.name, _err(e)))
        return out

    def _worker(self):
        while True:
            m = self.q.get()
            for ch in self.channels:
                for attempt in range(3):
                    try:
                        ch.send(m)
                        break
                    except urllib.error.HTTPError as e:
                        wait = 2 ** attempt
                        if e.code == 429:
                            try:
                                wait = int(json.loads(e.read()).get("parameters", {}).get("retry_after", wait))
                            except Exception:
                                pass
                        elif 400 <= e.code < 500:
                            self.last_error = "%s: %s" % (ch.name, _err(e)); log.error("alert failed on %s", self.last_error)
                            break
                        time.sleep(min(wait, 30))
                    except Exception as e:
                        if attempt == 2:
                            self.last_error = "%s: %s" % (ch.name, _err(e)); log.error("alert failed on %s", self.last_error)
                        time.sleep(2 ** attempt)
            time.sleep(0.3)   # stay well under Telegram's rate limits


def _err(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return "HTTP %s %s" % (e.code, e.read()[:200].decode("utf8", "ignore"))
        except Exception:
            return "HTTP %s" % e.code
    return str(e)[:200]


# ---------------------------------------------------------------- what to alert, and when
def usd(v):
    return "-" if v is None else "$%.2f" % v


def share_plan(s):
    """Shares to buy so that hitting the stop loses RISK_PCT of ACCOUNT_SIZE. None when no account size is set."""
    if not C.ACCOUNT_SIZE or not s.get("entry") or not s.get("stop"):
        return None
    per = s["entry"] - s["stop"]
    if per <= 0:
        return None
    risk = C.ACCOUNT_SIZE * C.RISK_PCT / 100
    sh = int(min(risk / per, C.ACCOUNT_SIZE / s["max_price"]))   # never more than the account can buy
    return dict(shares=sh, risk=sh * per, cost=sh * s["max_price"]) if sh > 0 else None


class AlertManager:
    """Turns scan results into a small number of useful notifications (no spam, no repeats after a restart)."""

    def __init__(self, notifier):
        self.n = notifier
        self.history = deque(maxlen=40)
        self.day = None
        self.sent = set()
        self.ready_seen = {}       # sym -> (trigger, consecutive scans)
        self.last_by_sym = {}      # sym -> (kind, trigger, time)
        self.armed = {}            # sym -> plan we alerted on, to warn if it fails
        self.stats = dict(ready=[], buy=[], failed=[])
        self.err_since = None
        self.err_sent = False
        self._load()

    # ---- persistence: alerts already sent today survive a restart
    def _load(self):
        try:
            with open(C.ALERT_STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("date") == datetime.now(C.NY).date().isoformat():
                self.day = datetime.now(C.NY).date()
                self.sent = set(st.get("sent", []))
                self.stats = st.get("stats", self.stats)
                self.history.extend(st.get("history", []))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("could not read alert state: %s", e)

    def _save(self):
        try:
            tmp = C.ALERT_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(date=self.day.isoformat(), sent=sorted(self.sent), stats=self.stats, history=list(self.history)), f)
            os.replace(tmp, C.ALERT_STATE_FILE)
        except Exception as e:
            log.warning("could not save alert state: %s", e)

    def _new_day(self, d):
        if self.day != d:
            self.day, self.sent = d, set()
            self.ready_seen.clear(); self.last_by_sym.clear(); self.armed.clear()
            self.stats = dict(ready=[], buy=[], failed=[])

    def _emit(self, key, kind, title, body, priority=3, tags=None):
        if key in self.sent:
            return False
        self.sent.add(key)
        self.history.appendleft(dict(t=datetime.now(C.NY).strftime("%H:%M"), kind=kind, title=title, body=body))
        self.n.send(title, body, priority, tags)
        self._save()
        return True

    # ---- public
    def startup(self):
        ch = ", ".join(self.n.names) or "none (dashboard only)"
        self.n.send("Momentum Desk is online",
                    "Mode: %s · data feed: %s\nAlerts: %s (%s)\nTrading window: %s-%s New York time." % (
                        "DEMO" if C.DEMO else "LIVE", "synthetic" if C.DEMO else C.FEED.upper(), ch,
                        "GET READY + BUY" if C.ALERT_LEVEL == "ready" else "BUY only",
                        C.TRADE_FROM.strftime("%H:%M"), C.TRADE_UNTIL.strftime("%H:%M")), priority=2, tags=["rocket"])

    def on_error(self, now, err):
        if err:
            self.err_since = self.err_since or now
            if not self.err_sent and now - self.err_since > timedelta(minutes=5):
                self.err_sent = True
                self.n.send("Scanner problem", "No fresh data for 5+ minutes.\nError: %s" % err[:300], 4, ["warning"])
        else:
            if self.err_sent:
                self.n.send("Scanner recovered", "Data is flowing again.", 2, ["white_check_mark"])
            self.err_since, self.err_sent = None, False

    def on_scan(self, now, stocks, ms, mood):
        self._new_day(now.date())
        if C.ALERT_MARKET_EVENTS:
            self._schedule(now, ms, stocks, mood)
        if ms.get("window_open"):
            self._stocks(now, stocks, mood)

    # ---- market schedule
    def _schedule(self, now, ms, stocks, mood):
        d = now.date().isoformat()
        today = ms.get("today")          # dict(open, close, win_start, win_end) as datetimes, or None if closed
        brief_at = datetime.combine(now.date(), C.BRIEF_AT, C.NY)
        if not today:
            if now.weekday() < 5 and brief_at <= now < brief_at + timedelta(hours=3) and not C.DEMO:
                self._emit(d + ":holiday", "market", "Market closed today", "US stock market holiday. No scanning today; enjoy the day off.", 2, ["calendar"])
            return
        mood_txt = ("\n" + mood["text"]) if mood else ""
        if brief_at <= now < min(today["open"], brief_at + timedelta(hours=3)):
            movers = [s for s in stocks if s["chg"] >= 10][:5]
            lines = ["%s %+.0f%% · %s" % (s["sym"], s["chg"], s["why"]) for s in movers] or ["No big gappers yet."]
            early = "\nEarly close today at %s New York time." % today["close"].strftime("%H:%M") if today["close"].hour < 16 else ""
            self._emit(d + ":brief", "market", "Morning brief: market opens at %s NY" % today["open"].strftime("%H:%M"),
                       "Top gappers so far:\n" + "\n".join(lines) + mood_txt + early +
                       "\nTrading window: %s-%s NY." % (today["win_start"].strftime("%H:%M"), today["win_end"].strftime("%H:%M")), 3, ["sunrise"])
        if today["open"] - timedelta(minutes=5) <= now < today["open"]:
            self._emit(d + ":open_soon", "market", "Market opens in 5 minutes", "Get your broker ready. Alerts will arrive here." + mood_txt, 3, ["bell"])
        if today["win_start"] <= now < today["win_start"] + timedelta(minutes=10):
            self._emit(d + ":window_open", "market", "Trading window is OPEN",
                       "Best hours for momentum: until %s NY.\nRules: stop before you buy, ~1%% risk per trade, stop after 3 losses." %
                       today["win_end"].strftime("%H:%M"), 3, ["green_circle"])
        if today["win_end"] <= now < today["win_end"] + timedelta(minutes=30):
            st = self.stats
            body = "Today: %d GET READY, %d BUY, %d failed setups." % (len(st["ready"]), len(st["buy"]), len(st["failed"]))
            if st["buy"]:
                body += "\nBuy signals: " + ", ".join(st["buy"])
            body += "\nMomentum usually fades now. Review your trades and the signals log."
            self._emit(d + ":window_close", "market", "Trading window closed", body, 2, ["checkered_flag"])

    # ---- stock signals
    def _stocks(self, now, stocks, mood):
        d = now.date().isoformat()
        seen = set()
        for s in stocks:
            sym, sig = s["sym"], s["signal"]
            seen.add(sym)
            # failed setups we told the user about
            a = self.armed.get(sym)
            if a and s["price"] < a["stop"]:
                self.armed.pop(sym, None)
                self.stats["failed"].append(sym)
                self._emit("%s:%s:fail:%s" % (d, sym, a["trigger"]), "fail", "SETUP FAILED · %s %s" % (sym, usd(s["price"])),
                           "Fell below the stop %s. If you bought, you should be out. If you didn't, cancel the plan." % usd(a["stop"]), 4, ["red_circle"])
                continue
            if not s.get("entry"):
                self.ready_seen.pop(sym, None)
                continue
            last = self.last_by_sym.get(sym)
            if sig in ("BUY NOW", "BUY SMALL"):
                if last and last[0] == "buy" and time.time() - last[2] < 300:
                    continue
                if self._emit("%s:%s:buy:%s" % (d, sym, s["trigger"]), "buy", self._title(s), self._body(s, mood), 5, ["green_circle", "moneybag"]):
                    self.last_by_sym[sym] = ("buy", s["trigger"], time.time())
                    self.armed[sym] = dict(stop=s["stop"], trigger=s["trigger"])
                    self.stats["buy"].append(sym)
            elif sig == "GET READY" and C.ALERT_LEVEL == "ready" and s["score"] >= C.ALERT_MIN_SCORE:
                trig, n = self.ready_seen.get(sym, (None, 0))
                n = n + 1 if trig == s["trigger"] else 1
                self.ready_seen[sym] = (s["trigger"], n)
                if n < 2:
                    continue          # must hold for 2 scans in a row: filters flicker
                if last and time.time() - last[2] < 600 and abs(s["trigger"] - last[1]) / last[1] < 0.02:
                    continue          # same idea alerted in the last 10 minutes
                if self._emit("%s:%s:ready:%s" % (d, sym, s["trigger"]), "ready", self._title(s), self._body(s, mood), 4, ["yellow_circle"]):
                    self.last_by_sym[sym] = ("ready", s["trigger"], time.time())
                    self.armed[sym] = dict(stop=s["stop"], trigger=s["trigger"])
                    self.stats["ready"].append(sym)
            else:
                self.ready_seen.pop(sym, None)
        for sym in list(self.ready_seen):
            if sym not in seen:
                self.ready_seen.pop(sym, None)

    @staticmethod
    def _title(s):
        icon = {"BUY NOW": "🟢", "BUY SMALL": "🟢", "GET READY": "🟡"}.get(s["signal"], "")
        return "%s %s · %s %s" % (icon, s["signal"], s["sym"], usd(s["price"]))

    @staticmethod
    def _body(s, mood):
        L = []
        if s["signal"] == "GET READY":
            L.append("Set a price alert at %s. Buy ONLY if it breaks above with strong volume." % usd(s["entry"]))
        elif s["signal"] == "BUY SMALL":
            L.append("Breaking out now. Use HALF size: some rules are missing.")
        else:
            L.append("Breaking out now. Limit order only.")
        L.append("Entry %s (never above %s) · Stop %s · Target %s" % (usd(s["entry"]), usd(s["max_price"]), usd(s["stop"]), usd(s["target"])))
        L.append("Risk %s/share · Reward %s:1 · Score %d (%s)" % (usd(s["entry"] - s["stop"]), s["rr"], s["score"], s["grade"]))
        sp = share_plan(s)
        if sp:
            if s["signal"] == "BUY SMALL":
                sp = dict(shares=sp["shares"] // 2, risk=sp["risk"] / 2)
            L.append("Size: %d shares (you risk %s if stopped)" % (sp["shares"], usd(sp["risk"])))
        L.append("Why: " + s["why"])
        if s.get("news"):
            L.append("News: " + s["news"][0]["h"][:140])
        bad = [t[1] for t in s.get("tips", []) if t[0] == "bad"]
        warn = [t[1] for t in s.get("tips", []) if t[0] == "warn"]
        if bad or warn:
            L.append("Careful: " + "; ".join((bad + warn)[:2]))
        if mood and mood.get("tone") == "bad":
            L.append("Weak market today: half size, take profits early.")
        return "\n".join(L)
