"""Paths and settings. Everything is overridable through environment variables or an optional `.env`
file in the project root (KEY=VALUE per line; real environment variables win). See README / `.env.example`.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    f = ROOT / ".env"
    if not f.is_file():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _flag(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() not in ("0", "false", "off", "no", "")


BOTS_DIR = ROOT / "bots"
DATA_DIR = Path(os.environ.get("FLEET_DATA_DIR") or ROOT / "data").expanduser()
CACHE_DIR = Path(os.environ.get("FLEET_CACHE_DIR") or ROOT / ".cache").expanduser()
DB_PATH = DATA_DIR / "fleet.db"
PORT = int(os.environ.get("FLEET_PORT", 8930))
HOST = os.environ.get("FLEET_HOST", "127.0.0.1")
if HOST not in ("127.0.0.1", "localhost"):   # security model: loopback only, no login system
    raise SystemExit(f"FLEET_HOST={HOST!r} refused: the server only binds to loopback (127.0.0.1)")
TASK_TIMEOUT = 15 * 60      # per-task limit (seconds)
ROUTER_MODEL = os.environ.get("FLEET_ROUTER_MODEL", "haiku")
ROUTER_TIMEOUT = 90

CTX_LIMIT = int(os.environ.get("FLEET_CTX_LIMIT", 150_000))   # rotate to a fresh conversation above this many context tokens
SESSION_MAX_AGE = 7 * 86400                                    # ...or when the conversation is older than 7 days
MEM_CAP = 20 * 1024                                            # max bytes of memory.md fed to a bot (tail is kept)
WRAPUP_TIMEOUT = 5 * 60

# Optional completion push via the Telegram Bot API (plain text). Off unless BOTH variables are set.
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TG_NOTIFY = bool(TG_TOKEN and TG_CHAT_ID) and _flag("FLEET_TG_NOTIFY", True)
TG_TIMEOUT = 10   # seconds; sending is async and never blocks a worker

# Optional: show your interactive Claude Code sessions as extra cats (read-only, local metadata only).
SESSIONS_ENABLED = _flag("FLEET_SESSIONS", False)
