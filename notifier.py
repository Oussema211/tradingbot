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
        self._stocks(now, stocks, mood)      # entries only happen inside the window; trade exits are sent any time

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
            body = "Today: %d GET READY, %d BUY, %d cancelled plans." % (len(st["ready"]), len(st["buy"]), len(st["failed"]))
            ts = getattr(self, "trades", None)
            if ts and ts.get("list"):
                body += "\nScoreboard: %d wins, %d losses, total %+.1fR" % (ts["wins"], ts["losses"], ts["r"])
                body += "\n" + "\n".join("%s %s %+.1fR" % (t["sym"], t["result"] or "open", t["r"] or 0) for t in ts["list"][:8])
            body += "\nMomentum usually fades now. Review your trades and the signals log."
            self._emit(d + ":window_close", "market", "Trading window closed", body, 2, ["checkered_flag"])

    # ---- stock signals
    def _stocks(self, now, stocks, mood):
        d = now.date().isoformat()
        seen = set()
        for s in stocks:
            sym, sig = s["sym"], s["signal"]
            seen.add(sym)
            tr = s.get("trade")
            if tr:
                self.armed.pop(sym, None)
                self._trade_events(d, s, tr)
            # a GET READY plan that broke down before triggering: nobody bought, just cancel the order
            a = self.armed.get(sym)
            if a and s["price"] < a["stop"]:
                self.armed.pop(sym, None)
                self.stats["failed"].append(sym)
                self._emit("%s:%s:cancel:%s" % (d, sym, a["trigger"]), "fail", "CANCEL PLAN · %s %s" % (sym, usd(s["price"])),
                           "It fell under %s before breaking out. Cancel your buy order. No trade, no loss." % usd(a["stop"]), 3, ["x"])
                continue
            if not s.get("entry"):
                self.ready_seen.pop(sym, None)
                continue
            last = self.last_by_sym.get(sym)
            if sig in ("BUY NOW", "BUY HALF") and tr:
                if self._emit("%s:%s:buy:%s" % (d, sym, tr["id"]), "buy", self._title(s), self._body(s, mood), 5, ["green_circle", "moneybag"]):
                    self.last_by_sym[sym] = ("buy", s["entry"], time.time())
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

    def _trade_events(self, d, s, tr):
        """Follow-up alerts for a BUY we sent: sell half, exit, target. Each is sent once."""
        sym, k = tr["sym"], "%s:%s:%s" % (d, tr["sym"], tr["id"])
        if tr.get("half"):
            self._emit(k + ":half", "trade", "💰 SELL HALF · %s %s" % (sym, usd(tr["half_price"])),
                       "Hit +1R. Sell HALF now.\nMove your stop UP to %s (breakeven): this trade can't lose anymore.\nRest: target %s." % (
                           usd(tr["entry"]), usd(tr["target"])), 5, ["moneybag"])
        if tr.get("closed"):
            win = tr["result"] == "WIN"
            title = ("✅ TARGET HIT · %s" if "target" in (tr["why"] or "") else "🔴 EXIT NOW · %s") % sym
            if tr["result"] == "BREAKEVEN":
                title = "⚪ EXIT · %s (breakeven)" % sym
            self._emit(k + ":close", "trade", title,
                       "%s at about %s: %s.\nResult: %s %+.1fR%s" % (
                           "Sell the rest" if tr.get("half") else "Sell everything", usd(tr["exit"]), tr["why"], tr["result"], tr["r"],
                           "" if win else "\nSmall loss, as planned. Don't re-enter this stock today."), 5, ["checkered_flag" if win else "red_circle"])

    @staticmethod
    def _title(s):
        icon = {"BUY NOW": "🟢", "BUY HALF": "🟢", "GET READY": "🟡"}.get(s["signal"], "")
        return "%s %s · %s %s" % (icon, s["signal"], s["sym"], usd(s["price"]))

    @staticmethod
    def _body(s, mood):
        L = [s.get("action") or ""]
        L.append("Entry %s (max %s) · Stop %s · Half at %s · Target %s" % (
            usd(s["entry"]), usd(s["max_price"]), usd(s["stop"]), usd(s["entry"] + (s["entry"] - s["stop"])), usd(s["target"])))
        sp = share_plan(s)
        if sp:
            if s["signal"] == "BUY HALF":
                sp = dict(shares=sp["shares"] // 2, risk=sp["risk"] / 2)
            L.append("Size: %d shares (you lose %s if stopped)" % (sp["shares"], usd(sp["risk"])))
        L.append("Checks passed: %d/%d · %s" % (s.get("passed", 0), s.get("total", 0), s["why"]))
        miss = [c[0] for c in s.get("checks", []) if not c[1] and not c[2]]
        if miss:
            L.append("Missing: " + ", ".join(miss[:3]))
        if s.get("news"):
            L.append("News: " + s["news"][0]["h"][:120])
        return "\n".join(L)
