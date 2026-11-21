"""
Real-time LEVEL 2 (order book) for Momentum Desk, from the Charles Schwab Trader API.

WHAT YOU GET
  * The real Nasdaq and NYSE order books: every price level with its share size and the market makers / venues on it.
  * The real best bid / ask (Level 1), so the spread is the true market spread, not the IEX sample.
  * A plain-English reading of the book (walls, imbalance, thin asks, hidden sellers, pulled orders) and,
    for every stock, ALL the possibilities: what it means if the ask gets eaten, if the bid breaks, if a wall vanishes...

SETUP (free with a Schwab brokerage account; see README.md, "Level 2")
  1. developer.schwab.com > create an app with "Market Data Production". Callback URL: https://127.0.0.1
  2. Put SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env.
  3. python momentum_scanner.py --schwab-login   (or use the "Connect Schwab" box in the dashboard)
  Schwab makes you log in again every 7 days. The app warns you a day before.

Only the standard library is used (the websocket client is built in). No orders are ever sent.
"""
import os, ssl, json, time, base64, socket, struct, hashlib, logging, threading, random, math
import urllib.request, urllib.parse, urllib.error
from collections import deque

import config as C

log = logging.getLogger("desk.l2")
API = "https://api.schwabapi.com"
AUTH_URL = API + "/v1/oauth/authorize"
TOKEN_URL = API + "/v1/oauth/token"
PREFS_URL = API + "/trader/v1/userPreference"
REFRESH_DAYS = 7          # Schwab refresh tokens die after 7 days: a new browser login is needed
BOOK_SVCS = ("NASDAQ_BOOK", "NYSE_BOOK")
STALE_S = 15              # a book older than this is not used for decisions


# ====================================================================== tiny websocket client (RFC 6455)
class WS:
    """Just enough websocket for a JSON stream: TLS, text frames, fragmentation, ping/pong. Timeout-safe reads."""
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, timeout=10):
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "wss" else 80)
        s = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: MomentumDesk\r\n\r\n" % (path, host, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise ConnectionError("websocket handshake: connection closed")
            buf += c
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError("websocket handshake refused: " + lines[0])
        want = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
        if not any(l.lower().startswith("sec-websocket-accept:") and l.split(":", 1)[1].strip() == want for l in lines):
            raise ConnectionError("websocket handshake: bad accept key")
        self.s, self.frag, self.wlock = s, [], threading.Lock()

    def settimeout(self, t):
        self.s.settimeout(t)

    def send(self, text, op=1):
        data = text.encode() if isinstance(text, str) else text
        n, mask = len(data), os.urandom(4)
        hdr = bytes([0x80 | op])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        with self.wlock:
            self.s.sendall(hdr + mask + body)

    def _frame(self):
        """Parse one frame from the buffer without consuming it unless complete (so a socket timeout loses nothing)."""
        b = self.buf
        if len(b) < 2:
            return None
        fin, op, masked, n, i = b[0] & 0x80, b[0] & 0x0F, b[1] & 0x80, b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        mk = None
        if masked:
            if len(b) < i + 4:
                return None
            mk, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        payload = b[i:i + n]
        if mk:
            payload = bytes(x ^ mk[j & 3] for j, x in enumerate(payload))
        self.buf = b[i + n:]
        return fin, op, payload

    def recv(self):
        """Next text message. Raises socket.timeout if nothing complete arrived in time, ConnectionError if closed."""
        while True:
            f = self._frame()
            if f is None:
                c = self.s.recv(65536)
                if not c:
                    raise ConnectionError("stream closed by Schwab")
                self.buf += c
                continue
            fin, op, payload = f
            if op == 9:
                self.send(payload, op=10)
            elif op == 8:
                raise ConnectionError("stream closed by Schwab (%s)" % payload[2:].decode("utf8", "ignore"))
            elif op in (0, 1, 2):
                self.frag.append(payload)
                if fin:
                    msg, self.frag = b"".join(self.frag), []
                    return msg.decode("utf8", "ignore")

    def close(self):
        try:
            self.send(b"", op=8)
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass


# ====================================================================== Schwab OAuth
class SchwabAuth:
    def __init__(self):
        self.tok, self.error, self.lock = {}, None, threading.RLock()
        self.load()

    @property
    def configured(self):
        return bool(C.SCHWAB_APP_KEY and C.SCHWAB_APP_SECRET)

    @property
    def connected(self):
        return bool(self.tok.get("refresh_token")) and self.days_left() > 0

    def load(self):
        try:
            with open(C.SCHWAB_TOKEN_FILE, encoding="utf-8") as f:
                self.tok = json.load(f)
        except FileNotFoundError:
            self.tok = {}
        except Exception as e:
            log.warning("could not read %s: %s", C.SCHWAB_TOKEN_FILE, e)
            self.tok = {}

    def save(self):
        tmp = C.SCHWAB_TOKEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.tok, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, C.SCHWAB_TOKEN_FILE)

    def login_url(self):
        return AUTH_URL + "?" + urllib.parse.urlencode(dict(client_id=C.SCHWAB_APP_KEY, redirect_uri=C.SCHWAB_CALLBACK))

    def days_left(self):
        t = self.tok.get("refresh_issued")
        return round(REFRESH_DAYS - (time.time() - t) / 86400, 2) if t else 0

    def _post(self, form):
        basic = base64.b64encode(("%s:%s" % (C.SCHWAB_APP_KEY, C.SCHWAB_APP_SECRET)).encode()).decode()
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), method="POST",
                                     headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError("Schwab said HTTP %s: %s" % (e.code, e.read()[:300].decode("utf8", "ignore")))

    def exchange(self, pasted):
        """Accepts the full address the browser landed on after login (https://127.0.0.1/?code=...) or just the code."""
        pasted = (pasted or "").strip()
        code = pasted
        if "code=" in pasted:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [""])[0]
        if not code:
            raise RuntimeError("No code found. Paste the whole address from the browser bar after you log in.")
        r = self._post(dict(grant_type="authorization_code", code=code, redirect_uri=C.SCHWAB_CALLBACK))
        with self.lock:
            now = time.time()
            self.tok = dict(access_token=r["access_token"], refresh_token=r["refresh_token"],
                            expires_at=now + int(r.get("expires_in", 1800)), refresh_issued=now)
            self.error = None
            self.save()
        log.info("Schwab: logged in. Level 2 is on. Next login needed in %d days.", REFRESH_DAYS)

    def access(self):
        """A valid access token (refreshed automatically every ~30 minutes)."""
        with self.lock:
            if not self.connected:
                raise RuntimeError("not logged in to Schwab" if not self.tok else "Schwab login expired (every 7 days)")
            if self.tok.get("expires_at", 0) - 120 > time.time():
                return self.tok["access_token"]
            try:
                r = self._post(dict(grant_type="refresh_token", refresh_token=self.tok["refresh_token"]))
            except RuntimeError as e:
                if "HTTP 400" in str(e) or "HTTP 401" in str(e):
                    self.tok["refresh_issued"] = 0          # dead: ask for a new login
                    self.save()
                    self.error = "Schwab login expired. Log in again (every 7 days)."
                raise
            self.tok.update(access_token=r["access_token"], expires_at=time.time() + int(r.get("expires_in", 1800)))
            if r.get("refresh_token"):
                self.tok["refresh_token"] = r["refresh_token"]
            self.save()
            return self.tok["access_token"]


def login_cli():
    """python momentum_scanner.py --schwab-login"""
    import sys, webbrowser
    if not (C.SCHWAB_APP_KEY and C.SCHWAB_APP_SECRET):
        sys.exit("Put SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env first (developer.schwab.com > your app). See README.md > Level 2.")
    a = SchwabAuth()
    url = a.login_url()
    print("\n1. Open this address and log in with your Schwab BROKERAGE login (not the developer one):\n\n   %s\n" % url)
    print("2. After 'Allow', the browser goes to %s and shows an error page. THAT IS NORMAL." % C.SCHWAB_CALLBACK)
    print("3. Copy the WHOLE address from the browser bar (it contains code=...) and paste it here.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    pasted = input("Paste the address: ")
    try:
        a.exchange(pasted)
    except Exception as e:
        sys.exit("Login failed: %s\n(The code is only valid for about 30 seconds: run this again and paste quickly.)" % e)
    print("\nDone. Level 2 is connected for the next 7 days. Start the app as usual.")


# ====================================================================== the live book
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _levels(raw):
    """Schwab book side -> [[price, shares, n_participants, [mpid, ...]], ...]"""
    out = []
    for lv in raw or []:
        p, sz = _num(lv.get("0")), _num(lv.get("1"))
        if p <= 0 or sz <= 0:
            continue
        ents = lv.get("3") or []
        mp = [str(e.get("0")) for e in sorted(ents, key=lambda e: -_num(e.get("1")))][:4]
        out.append([round(p, 4), int(sz), int(_num(lv.get("2")) or len(ents) or 1), mp])
    return out


class Level2:
    def __init__(self, demo=False):
        self.demo = demo
        self.auth = SchwabAuth()
        self.books, self.l1 = {}, {}             # (svc, sym) -> {"t", "bids", "asks"} ; sym -> {...}
        self.hist = {}                           # sym -> deque of (t, bb, ba, bbs, bas, last, vol, bids, asks)
        self.walls = {}                          # sym -> deque of (t, side, price, size)
        self.want, self.subbed = [], None
        self.lock = threading.RLock()
        self.status, self.err, self.connected_at, self.last_msg = "off", None, 0, 0
        self.warned_day = None

    # ---------------------------------------------------------------- public
    @property
    def enabled(self):
        return self.demo or self.auth.configured

    def public_status(self):
        a = self.auth
        st = dict(enabled=self.enabled, demo=self.demo, configured=a.configured, logged_in=a.connected,
                  days_left=a.days_left() if a.connected else 0, status=self.status, error=self.err or a.error,
                  live=self.demo or (self.status == "live" and time.time() - self.last_msg < 60), symbols=len(self.want))
        if a.configured and not a.connected:
            st["login_url"] = a.login_url()
            st["callback"] = C.SCHWAB_CALLBACK
        return st

    def watch(self, syms):
        with self.lock:
            self.want = list(dict.fromkeys(s for s in syms if s))[:C.L2_MAX_SYMBOLS]

    def start(self, stop_event, notifier=None):
        self.stop, self.notifier = stop_event, notifier
        if self.enabled:
            threading.Thread(target=self._demo_loop if self.demo else self._run, daemon=True, name="level2").start()

    def quote(self, sym):
        """Fresh real best bid/ask/last, or None."""
        with self.lock:
            q, b = self.l1.get(sym), self._book(sym)
        if not q or time.time() - q.get("t", 0) > STALE_S or not q.get("bid") or not q.get("ask"):
            return None
        out = dict(bp=q["bid"], ap=q["ask"], last=q.get("last"), bs=None, **{"as": None})
        if b and b["bids"] and b["asks"]:
            out["bs"], out["as"] = b["bids"][0][1], b["asks"][0][1]
        return out

    # ---------------------------------------------------------------- stream
    def _run(self):
        backoff = 5
        while not self.stop.is_set():
            if not self.auth.connected:
                self.status = "login needed"
                self.stop.wait(15)
                self.auth.load()          # picks up a login done with --schwab-login in another window
                continue
            if not self.want:
                self.status = "idle (no stocks to watch)"
                self.stop.wait(5)
                continue
            try:
                self._session()
                backoff = 5
            except Exception as e:
                self.err = str(e)[:200]
                self.status = "reconnecting"
                log.warning("Level 2 stream: %s (retry in %ds)", self.err, backoff)
                self.stop.wait(backoff)
                backoff = min(120, backoff * 2)
            finally:
                self.subbed = None

    def _session(self):
        tok = self.auth.access()
        req = urllib.request.Request(PREFS_URL, headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                prefs = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError("user preferences HTTP %s: %s" % (e.code, e.read()[:200].decode("utf8", "ignore")))
        si = (prefs.get("streamerInfo") or [{}])[0]
        ws = WS(si["streamerSocketUrl"])
        self.rid = 0
        ids = dict(SchwabClientCustomerId=si["schwabClientCustomerId"], SchwabClientCorrelId=si["schwabClientCorrelId"])

        def send(service, command, parameters):
            self.rid += 1
            ws.send(json.dumps({"requests": [dict(service=service, requestid=str(self.rid), command=command, parameters=parameters, **ids)]}))

        try:
            send("ADMIN", "LOGIN", dict(Authorization=tok, SchwabClientChannel=si["schwabClientChannel"],
                                        SchwabClientFunctionId=si["schwabClientFunctionId"]))
            ws.settimeout(20)
            while True:
                m = json.loads(ws.recv())
                r = next((x for x in m.get("response", []) if x.get("command") == "LOGIN"), None)
                if r:
                    code = (r.get("content") or {}).get("code")
                    if code != 0:
                        raise RuntimeError("Schwab stream login refused: %s" % (r.get("content") or {}).get("msg"))
                    break
            self.status, self.err, self.connected_at, self.last_msg = "live", None, time.time(), time.time()
            log.info("Level 2: connected to the Schwab stream.")
            ws.settimeout(1)
            while not self.stop.is_set():
                with self.lock:
                    want = tuple(self.want)
                if want != self.subbed:
                    keys = ",".join(want)
                    if keys:
                        for svc in BOOK_SVCS:
                            send(svc, "SUBS", dict(keys=keys, fields="0,1,2,3"))
                        send("LEVELONE_EQUITIES", "SUBS", dict(keys=keys, fields="0,1,2,3,8,9"))
                    self.subbed = want
                self._warn_expiry()
                if time.time() - self.last_msg > 90:
                    raise ConnectionError("no data or heartbeat for 90 s")
                try:
                    raw = ws.recv()
                except socket.timeout:
                    continue
                self.last_msg = time.time()
                try:
                    self._handle(json.loads(raw))
                except ValueError:
                    pass
        finally:
            ws.close()

    def _warn_expiry(self):
        d = self.auth.days_left()
        today = time.strftime("%Y-%m-%d")
        if d < 1 and self.warned_day != today and self.notifier:
            self.warned_day = today
            self.notifier.send("Schwab login expires soon",
                               "Level 2 stops in about %d hours. Log in again: python momentum_scanner.py --schwab-login "
                               "(or the Connect Schwab box on the dashboard)." % max(0, d * 24), priority=3)

    def _handle(self, m):
        for r in m.get("response", []):
            c = r.get("content") or {}
            if c.get("code") not in (0, None):
                log.warning("Level 2: %s %s refused: %s", r.get("service"), r.get("command"), c.get("msg"))
        now = time.time()
        for d in m.get("data", []):
            svc = d.get("service")
            for c in d.get("content") or []:
                sym = c.get("key")
                if not sym:
                    continue
                if svc in BOOK_SVCS:
                    with self.lock:
                        b = self.books.setdefault((svc, sym), dict(t=0, bids=[], asks=[]))
                        if "2" in c:
                            b["bids"] = sorted(_levels(c["2"]), key=lambda x: -x[0])
                        if "3" in c:
                            b["asks"] = sorted(_levels(c["3"]), key=lambda x: x[0])
                        b["t"] = now
                    self._snap(sym, now)
                elif svc == "LEVELONE_EQUITIES":
                    with self.lock:
                        q = self.l1.setdefault(sym, {})
                        for k, name in (("1", "bid"), ("2", "ask"), ("3", "last"), ("8", "vol"), ("9", "last_size")):
                            if k in c:
                                q[name] = _num(c[k])
                        q["t"] = now

    def _book(self, sym):
        """The freshest non-empty book for a symbol (Nasdaq book for Nasdaq stocks, NYSE book for NYSE stocks)."""
        best = None
        for svc in BOOK_SVCS:
            b = self.books.get((svc, sym))
            if b and (b["bids"] or b["asks"]) and (not best or len(b["bids"]) + len(b["asks"]) > len(best["bids"]) + len(best["asks"])):
                best = dict(b, src=svc.split("_")[0].title())
        return best

    def _snap(self, sym, now):
        """Keep ~2 minutes of book history (at most 2 snapshots a second) to spot hidden sellers and pulled walls."""
        with self.lock:
            b, q = self._book(sym), self.l1.get(sym, {})
            if not b:
                return
            h = self.hist.setdefault(sym, deque(maxlen=260))
            if h and now - h[-1][0] < 0.5:
                h.pop()
            bids, asks = b["bids"][:10], b["asks"][:10]
            h.append((now, bids[0][0] if bids else None, asks[0][0] if asks else None, bids[0][1] if bids else 0,
                      asks[0][1] if asks else 0, q.get("last"), q.get("vol"), [x[:2] for x in bids], [x[:2] for x in asks]))

    # ---------------------------------------------------------------- demo (fake book so you can see the panel at night)
    def _demo_loop(self):
        self.status = "live"
        while not self.stop.is_set():
            with self.lock:
                want = list(self.want)
                prices = dict(getattr(self, "demo_px", {}))
            now = time.time()
            for sym in want:
                p0 = prices.get(sym)
                if not p0:
                    continue
                rnd = random.Random(hash(sym) + int(now / 6))
                tick = 0.01 if p0 >= 1 else 0.0001
                q = self.l1.setdefault(sym, dict(vol=500000.0))
                last = round(p0 * (1 + random.uniform(-0.002, 0.002)), 2)
                bid, ask = round(last - tick * random.randint(0, 2), 2), round(last + tick * random.randint(1, 3), 2)
                wall_px = round(math.floor(p0) + 0.5 if p0 - math.floor(p0) < 0.5 else math.ceil(p0), 2)
                bids = [[round(bid - i * tick * rnd.choice((1, 1, 2, 3)), 2), rnd.choice((100, 200, 300, 500, 800, 1200, 2000)), rnd.randint(1, 4),
                         rnd.sample(["NSDQ", "ARCA", "EDGX", "CDRG", "MEMX", "BATS", "NITE", "VIRT", "GSCO"], 2)] for i in range(12)]
                asks = [[round(ask + i * tick * rnd.choice((1, 1, 2, 3)), 2), rnd.choice((100, 200, 300, 500, 800, 1200, 2000)), rnd.randint(1, 4),
                         rnd.sample(["NSDQ", "ARCA", "EDGX", "CDRG", "MEMX", "BATS", "NITE", "VIRT", "GSCO"], 2)] for i in range(12)]
                for side in (bids, asks):
                    side.sort(key=lambda x: x[0], reverse=side is bids)
                    seen, uniq = set(), []
                    for x in side:
                        if x[0] not in seen:
                            seen.add(x[0]); uniq.append(x)
                    side[:] = uniq
                for x in asks:
                    if abs(x[0] - wall_px) < tick / 2 or (rnd.random() < 0.08):
                        x[1] = rnd.choice((8000, 12000, 15000))
                if rnd.random() < 0.5:
                    bids[rnd.randint(2, 6)][1] = rnd.choice((6000, 9000, 14000))
                with self.lock:
                    self.books[("NASDAQ_BOOK", sym)] = dict(t=now, bids=bids, asks=asks)
                    q.update(bid=bid, ask=ask, last=last, vol=q["vol"] + random.randint(500, 6000), t=now)
                self._snap(sym, now)
            self.last_msg = now
            self.stop.wait(1)

    def demo_prices(self, px):
        with self.lock:
            self.demo_px = dict(px)

    # ================================================================== THE READING: plain English + all possibilities
    def read(self, sym, ctx=None):
        """Everything the dashboard shows for one stock. ctx = the scanner's view (price, entry, stop, max_price, t1, vwap, signal...)."""
        ctx = ctx or {}
        with self.lock:
            b, q = self._book(sym), dict(self.l1.get(sym) or {})
            h = list(self.hist.get(sym) or [])
        st = self.public_status()
        if not b:
            return dict(ok=False, sym=sym, status=st,
                        why="No order book yet for %s." % sym if st["live"] else "Level 2 is not connected.")
        now = time.time()
        age = now - b["t"]
        bids, asks = b["bids"][:12], b["asks"][:12]
        bb, ba = (bids[0][0] if bids else None), (asks[0][0] if asks else None)
        bid, ask = q.get("bid") or bb, q.get("ask") or ba
        last = q.get("last") or ctx.get("price") or (bid and ask and (bid + ask) / 2) or bb or ba
        mid = (bid + ask) / 2 if bid and ask else last
        u = lambda v: "$%.2f" % v if v is not None and v >= 1 else ("$%.4f" % v if v is not None else "-")
        sh = lambda n: "{:,}".format(int(n))
        cents = lambda v: ("%.0f¢" % (v * 100)) if v < 1 else "$%.2f" % v
        spread = (ask - bid) if bid and ask and ask >= bid else None
        spread_pct = spread / mid * 100 if spread is not None and mid else None

        near = lambda side: sum(x[1] for x in side if mid and abs(x[0] - mid) / mid <= 0.02)
        bid_near, ask_near = near(bids), near(asks)
        bid_tot, ask_tot = sum(x[1] for x in bids[:10]), sum(x[1] for x in asks[:10])
        imb = (bid_near / ask_near) if ask_near else (9.9 if bid_near else None)
        sizes = sorted(x[1] for x in bids[:10] + asks[:10])
        med = sizes[len(sizes) // 2] if sizes else 0
        wall_min = max(3 * med, 2000)
        # walls: levels 3x+ the normal size. The nearest one on each side matters most
        bwalls = [x for x in bids[:10] if x[1] >= wall_min]
        awalls = [x for x in asks[:10] if x[1] >= wall_min]
        bwall = bwalls[0] if bwalls else None
        awall = awalls[0] if awalls else None

        # air pockets: a gap of 1%+ between two ask (or bid) levels = price can jump through it
        def gap(side):
            for a, c in zip(side, side[1:6]):
                if mid and abs(c[0] - a[0]) / mid >= 0.01:
                    return a, c
            return None
        agap, bgap = gap(asks), gap(bids)

        # ---- history: hidden sellers / buyers (an iceberg refills at the same price) and walls that vanish
        hidden_ask = hidden_bid = None
        recent = [x for x in h if now - x[0] <= 30]
        if len(recent) >= 6 and recent[0][6] is not None and recent[-1][6] is not None:
            def iceberg(idx, size_idx, at_or_through):
                prices = {x[idx] for x in recent if x[idx]}
                if len(prices) != 1:
                    return None
                px = prices.pop()
                traded = 0
                for a, c in zip(recent, recent[1:]):
                    if c[5] is not None and a[6] is not None and c[6] is not None and at_or_through(c[5], px):
                        traded += max(0, c[6] - a[6])
                shown = max(x[size_idx] for x in recent)
                if shown and traded >= 2 * shown and recent[-1][size_idx] > 0:
                    return px, traded, recent[-1][size_idx]
                return None
            hidden_ask = iceberg(2, 4, lambda lp, px: lp >= px - 1e-9)
            hidden_bid = iceberg(1, 3, lambda lp, px: lp <= px + 1e-9)

        with self.lock:
            wq = self.walls.setdefault(sym, deque(maxlen=200))
            for side, w in (("ask", awall), ("bid", bwall)):
                if w and not any(abs(x[2] - w[0]) < 1e-6 and x[1] == side and now - x[0] < 3 for x in wq):
                    wq.append((now, side, w[0], w[1]))
            pulled, eaten = [], []
            lows = [x[5] for x in recent if x[5]]
            for t, side, px, sz in list(wq):
                if not (2 <= now - t <= 25):
                    continue
                cur = next((x[1] for x in (asks if side == "ask" else bids) if abs(x[0] - px) < 1e-6), 0)
                if cur >= 0.3 * sz or any(abs(p[2] - px) < 1e-6 and p[1] == side for p in pulled + eaten):
                    continue
                traded_through = any((lp >= px if side == "ask" else lp <= px) for lp in lows)
                (eaten if traded_through else pulled).append((t, side, px, sz))

        # ---- reads: [tone, title, detail]   tone: good / bad / warn / info
        R = []
        if age > STALE_S:
            R.append(["warn", "Book is %d s old" % age, "No update for a while: the stock may be halted or very quiet. Don't trust these levels."])
        if spread_pct is not None:
            if spread_pct > 2:
                R.append(["bad", "Wide spread %.2f%% (%s)" % (spread_pct, cents(spread)), "You lose that gap instantly on entry and exit. The app blocks trades over 2%. Limit orders only."])
            elif spread_pct > 1:
                R.append(["warn", "Spread %.2f%% (%s)" % (spread_pct, cents(spread)), "Acceptable but costly. Use limit orders, never market orders."])
            else:
                R.append(["good", "Tight spread %.2f%% (%s)" % (spread_pct, cents(spread)), "Real market spread. Cheap to get in and out."])
        if imb is not None:
            if imb >= 1.5:
                R.append(["good", "Buyers stacked (%.1fx more shares bid)" % imb,
                          "Within 2%% of the price: %s shares bid vs %s offered. Buyers are lining up below; dips likely get bought." % (sh(bid_near), sh(ask_near))])
            elif imb <= 0.67:
                R.append(["bad", "Sellers stacked (%.1fx more shares offered)" % (1 / imb if imb else 9.9),
                          "Within 2%% of the price: %s shares offered vs %s bid. Supply above is heavy; breakouts need a lot of buying." % (sh(ask_near), sh(bid_near))])
            else:
                R.append(["info", "Balanced book (%.1fx)" % imb, "Bids %s vs asks %s shares near the price. Neither side clearly in control; the tape decides." % (sh(bid_near), sh(ask_near))])
        if awall:
            rn = abs(awall[0] * 2 - round(awall[0] * 2)) < 1e-6
            R.append(["warn", "Big seller at %s (%s shares)" % (u(awall[0]), sh(awall[1])),
                      "%s the normal level size%s. It caps the price until buyers eat it. If it gets eaten fast, a squeeze often follows."
                      % ("%.0fx" % (awall[1] / med) if med else "Far above", ", on a round number (many sellers park orders there)" if rn else "")])
        if bwall:
            R.append(["good", "Big buyer at %s (%s shares)" % (u(bwall[0]), sh(bwall[1])),
                      "Support: sellers must get through it first. If it gets hit and disappears, price usually drops quickly to the next bids."])
        if agap and not awall:
            R.append(["good", "Thin above: gap %s to %s" % (u(agap[0][0]), u(agap[1][0])), "Few offers in between. If buyers lift %s, price can jump through the gap fast." % u(agap[0][0])])
        if bgap:
            R.append(["warn", "Thin below: gap %s to %s" % (u(bgap[0][0]), u(bgap[1][0])), "Few bids in between. If %s breaks, price can drop through the gap fast. Keep your stop honest." % u(bgap[0][0])])
        if hidden_ask:
            R.append(["bad", "Hidden seller at %s" % u(hidden_ask[0]),
                      "About %s shares traded there in 30 s but the offer keeps refilling (%s showing). A big seller is hiding size (iceberg). Don't buy until it's gone."
                      % (sh(hidden_ask[1]), sh(hidden_ask[2]))])
        if hidden_bid:
            R.append(["good", "Hidden buyer at %s" % u(hidden_bid[0]),
                      "About %s shares sold into it in 30 s and the bid keeps refilling. A big buyer is absorbing every seller: strong support." % sh(hidden_bid[1])])
        for t, side, px, sz in eaten[-2:]:
            R.append(["good" if side == "ask" else "bad", ("Seller at %s got eaten" if side == "ask" else "Buyer at %s got hit") % u(px),
                      ("%s shares were bought through. Resistance is gone: this is what a real breakout looks like." if side == "ask" else
                       "%s shares were sold through. Support broke: expect a quick drop to the next bids.") % sh(sz)])
        for t, side, px, sz in pulled[-2:]:
            R.append(["warn", ("Seller at %s pulled" if side == "ask" else "Buyer at %s pulled") % u(px),
                      ("%s shares vanished without trading. It may have been a fake wall to scare buyers, or the seller is waiting higher." if side == "ask" else
                       "%s shares vanished without trading. It was likely a fake bid (spoof) to lure buyers. Don't count on that support.") % sh(sz)])

        # ---- relative to the trade plan
        entry, stop, mx, t1 = ctx.get("entry"), ctx.get("stop"), ctx.get("max_price"), ctx.get("t1")
        to_entry = block = None
        if entry and asks:
            to_entry = sum(x[1] for x in asks if x[0] <= (mx or entry) + 1e-9)
            up = [x for x in asks if entry < x[0] <= (t1 or entry * 1.05) and x[1] >= wall_min]
            block = up[0] if up else None
            if block:
                R.append(["bad", "Seller between entry and +1R (%s)" % u(block[0]),
                          "%s shares sit at %s, before the first profit target %s. The trade must eat it to work." % (sh(block[1]), u(block[0]), u(t1))])
            elif to_entry is not None and mx:
                R.append(["info", "To trigger: %s shares up to %s" % (sh(to_entry), u(mx)),
                          "That's what buyers must take out to reach the entry zone. Small number = easy breakout; big = slow grind."])
        mms = {}
        for x in bids[:5] + asks[:5]:
            for m_ in x[3]:
                mms[m_] = mms.get(m_, 0) + 1
        if mms:
            top = sorted(mms.items(), key=lambda kv: -kv[1])[:4]
            R.append(["info", "Who's on the book", "Most active near the price: %s. NSDQ/ARCA/EDGX/BATS/MEMX are exchanges; NITE, VIRT, CDRG, GSCO etc. are market makers (they trade both ways, don't read too much into one)."
                      % ", ".join(k for k, _ in top)])

        # ---- ALL the possibilities from here
        P = []
        nb = bids[1][0] if len(bids) > 1 else None
        na = asks[1][0] if len(asks) > 1 else None
        if ba:
            tgt = (awall[0] if awall and awall[0] > ba else agap[1][0] if agap else na)
            P.append(["ok", "If buyers lift the ask %s" % u(ba),
                      "Price moves up%s. %s" % (" to the next seller at %s" % u(tgt) if tgt else "",
                                               "Real strength = the ask gets eaten AND bids move up behind it." )])
        if awall:
            P.append(["ok", "If the big seller at %s gets eaten" % u(awall[0]),
                      "Breakout signal: everyone who waited for it to clear buys at once. %s" % ("This is the BUY zone if it is your entry." if entry and abs(awall[0] - entry) / entry < 0.02 else "Expect a fast move to the next level.")])
            P.append(["warn", "If the seller at %s keeps refilling or gets bigger" % u(awall[0]),
                      "A hidden seller is capping it. Breakouts into it fail. Wait, or skip the stock."])
            P.append(["warn", "If the seller at %s disappears without trades" % u(awall[0]),
                      "It was probably fake (or moved higher). Don't buy just because it vanished; wait for real buying on the tape."])
        if bb:
            P.append(["bad", "If sellers hit the bid %s" % u(bb),
                      "Price ticks down%s. Fine if bids hold quickly; a warning if they keep dropping." % (" to %s" % u(nb) if nb else "")])
        if bwall:
            P.append(["ok", "If the big buyer at %s holds" % u(bwall[0]),
                      "Support works: dips stop there. A bounce off it is a lower-risk spot (your stop just under it)."])
            P.append(["bad", "If the big buyer at %s gets hit through" % u(bwall[0]),
                      "Support broke: expect a fast drop%s. Exit if you're long and the stop is near." % (" toward %s" % u(bgap[1][0]) if bgap else "")])
            P.append(["warn", "If the buyer at %s vanishes without trades" % u(bwall[0]),
                      "Fake support (spoof). The real bids are lower; risk is bigger than it looked."])
        if stop:
            P.append(["bad", "If it trades under %s (your stop)" % u(stop), "The setup is wrong. Exit or don't enter. The book doesn't change that rule."])
        if bb and ba:
            P.append(["info", "If nothing changes", "It keeps chopping between %s and %s. No trade inside the chop: wait for one side to break." % (u(bb), u(ba))])

        # ---- one-line verdict + the decision input for the checklist
        score = 0
        score += 1 if imb and imb >= 1.5 else -1 if imb is not None and imb <= 0.67 else 0
        score += 1 if bwall else 0
        score -= 1 if awall and (not entry or awall[0] <= (t1 or entry * 1.05)) else 0
        score -= 2 if hidden_ask else 0
        score += 1 if hidden_bid else 0
        score -= 2 if spread_pct is not None and spread_pct > 2 else 0
        score -= 1 if block else 0
        score += sum(1 if s == "ask" else -1 for _, s, _, _ in eaten[-2:])
        tone = "good" if score >= 1 else "bad" if score <= -1 else "warn"
        headline = {"good": "Book favors buyers", "bad": "Book favors sellers", "warn": "Book is neutral"}[tone]
        reasons = [r[1] for r in R if r[0] == ("good" if tone == "good" else "bad") and "spread" not in r[1].lower()][:2] or [r[1] for r in R if r[0] in ("good", "bad")][:1]
        fresh = age <= STALE_S
        against = (imb is not None and imb <= 0.67) or bool(hidden_ask) or bool(block) or (spread_pct is not None and spread_pct > 2)
        check = (None, "no fresh book") if not fresh else (not against, "; ".join(
            x for x in ["sellers stacked" if imb is not None and imb <= 0.67 else "", "hidden seller" if hidden_ask else "",
                        "seller at %s before +1R" % u(block[0]) if block else "", "spread %.1f%%" % spread_pct if spread_pct and spread_pct > 2 else ""] if x)
            or ("bids %.1fx asks" % imb if imb is not None else "ok"))

        return dict(ok=True, sym=sym, src=b.get("src"), age=round(age, 1), status=st, fresh=fresh,
                    bid=bid, ask=ask, last=last, spread=spread and round(spread, 4), spread_pct=spread_pct and round(spread_pct, 2),
                    bids=bids, asks=asks, bid_near=bid_near, ask_near=ask_near, bid_tot=bid_tot, ask_tot=ask_tot,
                    imb=imb and round(imb, 2), wall_min=wall_min, bwall=bwall and bwall[0], awall=awall and awall[0],
                    tone=tone, headline=headline + (": " + ", ".join(reasons).lower() if reasons else ""),
                    reads=R, possible=P, check=check, to_entry=to_entry,
                    entry=entry, stop=stop, max_price=mx, t1=t1)
