"""SQLite:bots 的 session、tasks、events。同步 sqlite3 + 一把鎖,夠用也好讀。"""
import json
import sqlite3
import threading
import time

from .config import DATA_DIR, DB_PATH

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_state(bot TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT, bot TEXT, status TEXT,          -- queued/running/done/error/cancelled/unassigned
  route_reason TEXT, route_by TEXT,          -- route_by: router / manual
  result TEXT, session_id TEXT,
  created REAL, started REAL, finished REAL,
  input_tokens INT DEFAULT 0, output_tokens INT DEFAULT 0,
  cache_read INT DEFAULT 0, cache_create INT DEFAULT 0,
  cost_usd REAL DEFAULT 0, duration_ms INT DEFAULT 0, resumed INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, task_id INT, bot TEXT, kind TEXT, data TEXT);   -- data = JSON
CREATE INDEX IF NOT EXISTS ev_task ON events(task_id);
"""


def init():
    global _conn
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    for col in ("ctx_tokens INT DEFAULT 0", "session_started REAL"):   # 輕量 migration
        try:
            _conn.execute(f"ALTER TABLE bot_state ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    try:   # 完成推播旗標:1=從網頁/./fleet task 送出(完成時推 Telegram);./fleet ask 與系統任務為 0
        _conn.execute("ALTER TABLE tasks ADD COLUMN notify INT DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 重啟時,上次殘留的 queued/running 任務標成中斷(v0 不自動重跑)
    _conn.execute("UPDATE tasks SET status='error', result='服務重啟,任務中斷', finished=COALESCE(finished,?) WHERE status IN('queued','running','routing')", (time.time(),))
    _conn.commit()


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def get_session(bot):
    r = q("SELECT session_id FROM bot_state WHERE bot=?", (bot,))
    return r[0]["session_id"] if r else None


def get_state(bot):
    r = q("SELECT session_id, COALESCE(ctx_tokens,0) ctx, session_started FROM bot_state WHERE bot=?", (bot,))
    return r[0] if r else {"session_id": None, "ctx": 0, "session_started": None}


def set_session(bot, sid, ctx=0):
    """session_started 只在「原本沒有 session」時重設(同一段對話的 --resume 不重算年齡)。"""
    old = get_state(bot)
    started = old["session_started"] if old["session_id"] and old["session_started"] else time.time()
    x("INSERT INTO bot_state(bot,session_id,ctx_tokens,session_started) VALUES(?,?,?,?) ON CONFLICT(bot) DO UPDATE SET "
      "session_id=excluded.session_id, ctx_tokens=excluded.ctx_tokens, session_started=excluded.session_started",
      (bot, sid, ctx, started))


def add_event(task_id, bot, kind, data):
    ts = time.time()
    eid = x("INSERT INTO events(ts,task_id,bot,kind,data) VALUES(?,?,?,?,?)",
            (ts, task_id, bot, kind, json.dumps(data, ensure_ascii=False)))
    return {"id": eid, "ts": ts, "task_id": task_id, "bot": bot, "kind": kind, "data": data}
