"""
Settings for Momentum Desk.

Everything is read from environment variables, or from a file named `.env` next to this file
(one KEY=value per line). Copy `.env.example` to `.env` and fill it in. Never share `.env`.
"""
import os, sys
from datetime import time as dtime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Minimal .env reader (no extra packages needed). Real environment variables win over the file."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)


_load_dotenv(os.path.join(HERE, ".env"))


def env(name, default=""):
    return os.getenv(name, default).strip()


def env_bool(name, default=False):
    v = env(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


def env_float(name, default):
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_time(name, default):
    try:
        h, m = env(name, default).split(":")
        return dtime(int(h), int(m))
    except ValueError:
        h, m = default.split(":")
        return dtime(int(h), int(m))


NY = ZoneInfo("America/New_York")

# ---- data (Alpaca) -------------------------------------------------------
KEY = env("APCA_API_KEY_ID")
SEC = env("APCA_API_SECRET_KEY")
FEED = env("ALPACA_FEED", "iex").lower()          # iex = free (partial market), sip = paid (full market)
WATCH = [s.strip().upper() for s in env("WATCHLIST").split(",") if s.strip()]

# ---- trading window (New York time) --------------------------------------
TRADE_FROM = env_time("TRADE_FROM", "09:30")     # set 07:00 to include pre-market setups
TRADE_UNTIL = env_time("TRADE_UNTIL", "11:00")
BRIEF_AT = env_time("MORNING_BRIEF_AT", "09:00")  # morning summary notification

# ---- risk (used for share counts in alerts; the dashboard lets each viewer override) --
ACCOUNT_SIZE = env_float("ACCOUNT_SIZE", 0)        # 0 = don't show share counts in alerts
RISK_PCT = env_float("RISK_PCT", 1.0)              # % of the account you accept to lose per trade

# ---- notifications --------------------------------------------------------
TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT = env("TELEGRAM_CHAT_ID")
NTFY_TOPIC = env("NTFY_TOPIC")
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOKEN = env("NTFY_TOKEN")
DISCORD_WEBHOOK = env("DISCORD_WEBHOOK_URL")
ALERT_LEVEL = env("ALERT_LEVEL", "ready").lower()  # ready = GET READY + BUY, buy = only BUY signals
ALERT_MIN_SCORE = int(env_float("ALERT_MIN_SCORE", 60))
ALERT_MARKET_EVENTS = env_bool("ALERT_MARKET_EVENTS", True)
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")         # e.g. https://desk.example.com, added as a link in alerts

# ---- server ---------------------------------------------------------------
DEMO = "--demo" in sys.argv or env_bool("DEMO")
HOST = env("HOST", "127.0.0.1")                   # 0.0.0.0 to accept connections from other machines
PORT = int(env_float("PORT", 8765))
DASHBOARD_PASSWORD = env("DASHBOARD_PASSWORD")
HEADLESS = "--no-browser" in sys.argv or env_bool("HEADLESS") or HOST not in ("127.0.0.1", "localhost")
INTERVAL = 3 if DEMO else max(5, int(env_float("SCAN_INTERVAL", 10)))
DATA_DIR = env("DATA_DIR") or HERE
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
# Exit by itself at this New York time (for scheduled runs such as GitHub Actions). Empty = run forever.
_ru = env("RUN_UNTIL")
for _a in sys.argv:
    if _a.startswith("--until="):
        _ru = _a.split("=", 1)[1]
RUN_UNTIL = env_time("_UNUSED", _ru) if _ru else None

os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "signals_log.csv")
ALERT_STATE_FILE = os.path.join(DATA_DIR, "alert_state.json")
WEB_FILE = os.path.join(HERE, "web", "index.html")
