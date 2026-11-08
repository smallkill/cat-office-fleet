"""FastAPI 主程式:API + SSE + 每個 Bot 一條佇列的執行器 + 靜態儀表板。"""
import asyncio
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import bots as botlib, config, db, runner, sessions

BOTS = {}          # id -> bot 定義(BOT.md)
ROTATING = {}      # bot -> 進行中的 rotate 數(>0 顯示 rotating); 正在收尾/換新對話的 Bot(儀表板顯示 rotating)
RT = {}            # id -> 即時狀態 {status, action, task_id}
QUEUES = {}        # id -> asyncio.Queue[task_id]
PROCS = {}         # task_id -> 正在跑的 subprocess(取消用)
CANCELLED = set()
SUBS = []          # SSE 訂閱者
TOKEN = ""         # 啟動時隨機產生,寫到 .cache/token(0600)
ALLOWED_HOSTS = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
SESSIONS = []      # latest scan of local interactive Claude Code sessions (optional feature)


# ---------- 廣播 ----------
def push(msg):
    for q in list(SUBS):
        q.put_nowait(msg)


def emit(task_id, bot, kind, data):
    """寫進 SQLite 並推給 SSE。"""
    ev = db.add_event(task_id, bot, kind, data)
    push({"type": "event", "event": ev})
    return ev


def set_rt(bot, status, action="", task_id=None):
    RT[bot] = {"status": status, "action": action, "task_id": task_id}
    push({"type": "bots", "bots": bots_view()})


def bots_view():
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out = []
    for b in BOTS.values():
        r = RT.get(b["id"], {"status": "idle", "action": "", "task_id": None})
        row = db.q("SELECT COALESCE(SUM(input_tokens+output_tokens+cache_create),0) t, COALESCE(SUM(cache_read),0) c,"
                   " COALESCE(SUM(cost_usd),0) usd, COUNT(*) n FROM tasks WHERE bot=? AND created>=?", (b["id"], midnight))[0]
        st = db.get_state(b["id"]) if not b["is_router"] else None
        out.append({
            "ctx_tokens": st["ctx"] if st else 0, "ctx_limit": config.CTX_LIMIT,
            "memory_bytes": botlib.memory_bytes(b) if b["memory"] else None, "memory_cap": config.MEM_CAP,
            "session_age_days": round((time.time() - st["session_started"]) / 86400, 2) if st and st["session_id"] and st["session_started"] else None,
            "has_memory": b["memory"],
            "id": b["id"], "name": b["name"], "emoji": b["emoji"], "description": b["description"],
            "is_router": b["is_router"], "tools": b["builtin"],
            "status": "rotating" if ROTATING.get(b["id"]) else r["status"],
            "action": ("收尾並換新對話中…" if ROTATING.get(b["id"]) else r["action"]), "task_id": r["task_id"],
            "queued": QUEUES[b["id"]].qsize() if b["id"] in QUEUES else 0,
            "tokens_today": row["t"], "cache_read_today": row["c"], "cost_today": round(row["usd"], 4),
            "tasks_today": row["n"],
            "session_id": db.get_session(b["id"]) if not b["is_router"] else None,
        })
    return out


# ---------- Completion push (optional Telegram) ----------
NOTIFIED = set()   # task ids already pushed (one push per task; retry/rotate never repeats)


def build_notice(task, bot):
    """Plain-text message (no parse_mode, so no escaping needed)."""
    one = lambda t, n: (t if len(t) <= n else t[:n - 1] + "…")
    prompt = one(" ".join((task["text"] or "").split()), 60)
    ok = task["status"] == "done"
    body = (task["result"] or "").strip()
    lines = [l for l in body.splitlines() if l.strip()][:5]
    snippet = one("\n".join(lines), 400)
    return (f"{bot['emoji']} {bot['name']} {'done' if ok else 'failed'}\n"
            f"Task #{task['id']}: {prompt}\n\n{snippet}")


def _send_telegram(text):
    """Blocking Bot API call (run in a thread). Raises on failure; the caller scrubs the token from messages."""
    data = urllib.parse.urlencode({"chat_id": config.TG_CHAT_ID, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=config.TG_TIMEOUT) as r:
        r.read(1024)


async def notify_done(task_id):
    """Push a plain-text summary when a task reaches done/error. Never raises, never blocks a worker
    (callers use create_task). Disabled unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    try:
        if not config.TG_NOTIFY or task_id in NOTIFIED:
            return
        rows = db.q("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            return
        t = rows[0]
        if not t.get("notify") or t["route_by"] == "system" or t["status"] not in ("done", "error") or t["bot"] not in BOTS:
            return
        NOTIFIED.add(task_id)
        await asyncio.wait_for(asyncio.to_thread(_send_telegram, build_notice(t, BOTS[t["bot"]])), config.TG_TIMEOUT + 5)
    except Exception as e:   # a failed push is only logged
        msg = str(e).replace(config.TG_TOKEN or "\0", "***")
        print(f"telegram notify #{task_id} failed: {type(e).__name__}: {msg[:200]}")


NOTIFY_TASKS = set()


def fire_notify(task_id):
    if isinstance(task_id, int) and config.TG_NOTIFY:
        t = asyncio.get_running_loop().create_task(notify_done(task_id))
        NOTIFY_TASKS.add(t)   # keep a strong reference so it is not GC'd mid-send
        t.add_done_callback(NOTIFY_TASKS.discard)


# ---------- 執行器 ----------
async def worker_loop(bot_id):
    """每個 Bot 一條佇列、一次只做一件事。任何例外都不能讓這個迴圈死掉。"""
    while True:
        item = await QUEUES[bot_id].get()
        is_rot = isinstance(item, tuple)              # ("rotate", 理由, rotate 任務 id)
        task_id = item[2] if is_rot else item
        try:
            if is_rot:                                # 手動「收尾並換新對話」
                await rotate(bot_id, item[1], tid=task_id)
                continue
            why = rotation_reason(bot_id)             # 新任務開跑前檢查:context 太大/對話太老 → 先收尾換新
            if why:
                try:
                    await rotate(bot_id, why)
                except Exception as e:                # 收尾本身壞了不影響這個任務照跑
                    print("rotate error:", e)
            await run_task(bot_id, task_id)
            fire_notify(task_id)
        except Exception as e:  # 保底:單一任務爆掉不能讓 worker 死掉
            try:
                if isinstance(task_id, int):          # 只有真正的 int id 才碰 DB
                    db.x("UPDATE tasks SET status='error', result=?, finished=? WHERE id=?", (str(e), time.time(), task_id))
                    emit(task_id, bot_id, "error", {"message": str(e)})
                set_rt(bot_id, "error", str(e)[:100], task_id if isinstance(task_id, int) else None)
                fire_notify(task_id)
            except Exception as e2:
                print("worker handler error:", e2)


def rotation_reason(bot_id):
    st = db.get_state(bot_id)
    if not st["session_id"]:
        return None
    if st["ctx"] > config.CTX_LIMIT:
        return f"context {st['ctx'] // 1000}k 超過 {config.CTX_LIMIT // 1000}k"
    if st["session_started"] and time.time() - st["session_started"] > config.SESSION_MAX_AGE:
        return f"對話已 {(time.time() - st['session_started']) / 86400:.1f} 天,超過 {config.SESSION_MAX_AGE // 86400} 天"
    return None


WRAPUP_PROMPT = """This conversation is about to end (wrap-up). The next one starts fresh and will only see BOT.md and memory.md.
Tidy your long-term memory file memory.md now (Read it first, then Edit it):
1. Keep only durable information: the user's decisions and preferences, ongoing work, lessons learned, standing procedures.
2. Do not store volatile numbers (prices, balances, counts, dates-sensitive facts); re-read the source next time instead.
3. Merge in anything from this conversation worth keeping long-term; merge duplicates; delete outdated or finished items.
4. Keep the whole file concise: under 18KB in total (shorter is better), as bullet points, in the language the user uses.
5. If nothing needs to change, do not edit.
When done, reply with a single line saying what changed (e.g. "added 2, merged 1, removed 3")."""


ROTATE_FAILS = {}   # bot -> 連續收尾失敗次數;失敗 2 次就不再保留 session,直接換新(已有備份)
MAX_ROTATE_FAILS = 2


async def rotate(bot_id, reason, tid=None):
    """收尾 + 換新對話:在舊 session 上跑一輪「整理記憶」,成功才清掉 session_id。
    收尾失敗/被取消 → 保留 session、發 rotate_failed,下個任務前自動再試;連續失敗 2 次就強制換新。
    有記憶的 Bot 才收尾(news 直接換)。回傳是否已換新。"""
    bot = BOTS[bot_id]
    ROTATING[bot_id] = ROTATING.get(bot_id, 0) + 1
    try:
        push({"type": "bots", "bots": bots_view()})
        if tid is None:
            tid = db.x("INSERT INTO tasks(text,bot,status,route_reason,route_by,created) VALUES(?,?,?,?,?,?)",
                       (f"[收尾並換新對話] {reason}", bot_id, "queued", reason, "system", time.time()))
        st = db.get_state(bot_id)
        if not st["session_id"]:
            db.x("UPDATE tasks SET status='done', result='目前沒有對話可收尾', finished=? WHERE id=?", (time.time(), tid))
            return False
        before = botlib.memory_bytes(bot)
        wrapped = None
        if bot["memory"]:
            mem_file = config.BOTS_DIR / bot_id / "memory.md"
            bk = config.CACHE_DIR / "memory-backup"
            bk.mkdir(mode=0o700, exist_ok=True)
            if mem_file.exists():   # 收尾前備份:Bot 改壞(或被投毒改寫)時可還原
                (bk / f"{bot_id}-{time.strftime('%Y%m%d-%H%M%S')}.md").write_text(mem_file.read_text(encoding="utf-8"), encoding="utf-8")
            try:
                await run_task(bot_id, tid, prompt=WRAPUP_PROMPT, allow_retry=False, timeout=config.WRAPUP_TIMEOUT)
            except Exception as e:
                db.x("UPDATE tasks SET status='error', result=?, finished=? WHERE id=?", (str(e), time.time(), tid))
            wrapped = db.q("SELECT status FROM tasks WHERE id=?", (tid,))[0]["status"] == "done"
            if not wrapped:
                n = ROTATE_FAILS[bot_id] = ROTATE_FAILS.get(bot_id, 0) + 1
                if n < MAX_ROTATE_FAILS:
                    emit(tid, bot_id, "rotate_failed", {"reason": reason, "wrapup": False, "fails": n,
                                                        "note": "收尾失敗,保留舊對話,下個任務前會再試"})
                    return False
        else:
            db.x("UPDATE tasks SET status='done', result=?, started=?, finished=? WHERE id=?",
                 ("此 Bot 沒有記憶,直接換新對話", time.time(), time.time(), tid))
        ROTATE_FAILS.pop(bot_id, None)
        db.x("DELETE FROM bot_state WHERE bot=?", (bot_id,))   # 下一個任務開新 session
        emit(tid, bot_id, "rotated", {"reason": reason, "wrapup": wrapped, "forced": wrapped is False,
                                      "memory_before": before, "memory_after": botlib.memory_bytes(bot),
                                      "old_session": st["session_id"], "ctx": st["ctx"]})
        return True
    finally:
        # 不論如何,這筆 rotate 任務不能卡在 queued/running
        if tid is not None:
            db.x("UPDATE tasks SET status='error', result=COALESCE(result,'收尾中斷'), finished=? WHERE id=? AND status IN('queued','running')",
                 (time.time(), tid))
        if ROTATING.get(bot_id, 0) <= 1:
            ROTATING.pop(bot_id, None)
        else:
            ROTATING[bot_id] -= 1
        set_rt(bot_id, "idle", "", None)


async def run_task(bot_id, task_id, allow_retry=True, prompt=None, timeout=None):
    timeout = timeout or config.TASK_TIMEOUT
    bot = BOTS[bot_id]
    task = db.q("SELECT * FROM tasks WHERE id=?", (task_id,))[0]
    if task["status"] == "cancelled":
        set_rt(bot_id, "idle")
        return
    sid = db.get_session(bot_id)
    db.x("UPDATE tasks SET status='running', started=?, resumed=? WHERE id=?", (time.time(), 1 if sid else 0, task_id))
    emit(task_id, bot_id, "task_started", {"resumed": bool(sid), "session_id": sid})
    set_rt(bot_id, "thinking", "思考中…", task_id)

    cmd = runner.worker_cmd(bot, sid, botlib.memory_text(bot))
    proc = await runner.spawn(cmd, bot["dir"], prompt or task["text"], limit=16 * 1024 * 1024)
    PROCS[task_id] = proc                      # 先登記,cancel 才找得到
    if task_id in CANCELLED:                   # 登記前就被取消的競態
        await runner.kill_group(proc)
    final = None
    tools = {}   # tool_use_id -> name
    errtext = []
    state = {"sid": None, "ctx": 0}

    async def read_stderr():
        errtext.append((await proc.stderr.read()).decode(errors="replace"))
    err_task = asyncio.create_task(read_stderr())

    async def read_stdout():
        nonlocal final
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            final = handle_stream_event(bot_id, task_id, ev, tools, state) or final

    try:
        await asyncio.wait_for(read_stdout(), timeout)
    except asyncio.TimeoutError:
        emit(task_id, bot_id, "error", {"message": f"逾時({timeout}s),已終止"})
        final = None
    finally:
        await runner.kill_group(proc)          # 正常結束時是 no-op;逾時/取消時殺整個 process group
        PROCS.pop(task_id, None)
        await err_task

    if task_id in CANCELLED:
        CANCELLED.discard(task_id)
        db.x("UPDATE tasks SET status='cancelled', finished=? WHERE id=?", (time.time(), task_id))
        emit(task_id, bot_id, "cancelled", {})
        set_rt(bot_id, "idle", "", None)
        return

    stderr = "".join(errtext)
    # --resume 指到不存在的 session(例如被清掉):清掉 session 重試一次
    if final is None and sid and allow_retry and "No conversation found" in stderr:
        db.x("DELETE FROM bot_state WHERE bot=?", (bot_id,))
        emit(task_id, bot_id, "text", {"text": "(舊 session 不存在,改開新 session)"})
        return await run_task(bot_id, task_id, allow_retry=False)

    if final is None:
        msg = (stderr.strip() or "claude 沒有回傳結果")[:500]
        db.x("UPDATE tasks SET status='error', result=?, finished=? WHERE id=?", (msg, time.time(), task_id))
        emit(task_id, bot_id, "error", {"message": msg})
        set_rt(bot_id, "error", msg[:100], task_id)
        return

    u = final.get("usage", {})
    status = "error" if final.get("is_error") else "done"
    db.x("UPDATE tasks SET status=?, result=?, session_id=?, finished=?, input_tokens=?, output_tokens=?, cache_read=?,"
         " cache_create=?, cost_usd=?, duration_ms=? WHERE id=?",
         (status, final.get("result", ""), final.get("session_id"), time.time(), u.get("input_tokens", 0),
          u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0),
          final.get("total_cost_usd", 0), final.get("duration_ms", 0), task_id))
    if final.get("session_id") and not final.get("is_error"):
        db.set_session(bot_id, final["session_id"], state["ctx"])   # 只有成功的 run 才記住 session,失敗不污染
    emit(task_id, bot_id, "result", {
        "ok": status == "done", "text": final.get("result", ""), "duration_ms": final.get("duration_ms"),
        "cost_usd": final.get("total_cost_usd"), "usage": u, "session_id": final.get("session_id")})
    set_rt(bot_id, status, "完成" if status == "done" else "失敗", task_id)


def handle_stream_event(bot_id, task_id, ev, tools, state):
    """把 claude 的 stream-json 一行轉成我們的事件。回傳 result 事件(若是)。"""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        state["sid"] = ev.get("session_id")     # 先暫存;run 成功結束才寫入 DB
    elif t == "assistant":
        mu = ev.get("message", {}).get("usage") or {}   # 最後一次 API 呼叫的輸入 = 目前對話 context 大小
        c = mu.get("input_tokens", 0) + mu.get("cache_read_input_tokens", 0) + mu.get("cache_creation_input_tokens", 0)
        if c:
            state["ctx"] = c
        for blk in ev.get("message", {}).get("content", []):
            if blk.get("type") == "text" and blk.get("text", "").strip():
                emit(task_id, bot_id, "text", {"text": blk["text"]})
            elif blk.get("type") == "tool_use":
                tools[blk["id"]] = blk["name"]
                s = runner.short(blk["name"], blk.get("input"))
                emit(task_id, bot_id, "tool_use", {"tool": blk["name"], "summary": s})
                set_rt(bot_id, "working", f"🔧 {blk['name']}: {s}", task_id)
    elif t == "user":
        content = ev.get("message", {}).get("content", [])
        for blk in content if isinstance(content, list) else []:
            if blk.get("type") == "tool_result":
                body = blk.get("content")
                if isinstance(body, list):
                    body = " ".join(c.get("text", "") for c in body if isinstance(c, dict))
                emit(task_id, bot_id, "tool_result", {
                    "tool": tools.get(blk.get("tool_use_id"), "?"), "ok": not blk.get("is_error"),
                    "preview": str(body or "")[:300]})
                set_rt(bot_id, "thinking", "思考中…", task_id)
    elif t == "result":
        return ev
    return None


# ---------- 任務入口 ----------
async def submit(text, bot_override=None, notify=True):
    if bot_override:
        if bot_override not in BOTS or BOTS[bot_override]["is_router"]:
            raise HTTPException(400, f"未知的 bot: {bot_override}")
        bot, reason, by = bot_override, "使用者指定", "manual"
        tid = db.x("INSERT INTO tasks(text,bot,status,route_reason,route_by,created,notify) VALUES(?,?,?,?,?,?,?)",
                   (text, bot, "queued", reason, by, time.time(), int(notify)))
    else:
        tid = db.x("INSERT INTO tasks(text,bot,status,route_by,created,notify) VALUES(?,?,?,?,?,?)",
                   (text, "router", "routing", "router", time.time(), int(notify)))
        set_rt("router", "thinking", "分派中…", tid)
        emit(tid, "router", "task_started", {"text": text})
        bot, reason, res = await runner.run_router(text, BOTS)
        u = res.get("usage", {}) if res else {}
        db.x("UPDATE tasks SET input_tokens=?, output_tokens=?, cache_read=?, cache_create=?, cost_usd=? WHERE id=?",
             (u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0),
              u.get("cache_creation_input_tokens", 0), res.get("total_cost_usd", 0) if res else 0, tid))
        by = "router"
        if bot == "unassigned":
            db.x("UPDATE tasks SET bot=?, status='unassigned', route_reason=? WHERE id=?", (bot, reason, tid))
            emit(tid, "router", "routed", {"bot": bot, "reason": reason, "by": by})
            set_rt("router", "idle", "", None)
            return tid
        db.x("UPDATE tasks SET bot=?, status='queued', route_reason=? WHERE id=?", (bot, reason, tid))
        set_rt("router", "done", f"→ {BOTS[bot]['emoji']} {BOTS[bot]['name']}", tid)
    emit(tid, bot, "routed", {"bot": bot, "reason": reason, "by": by})
    QUEUES[bot].put_nowait(tid)
    push({"type": "bots", "bots": bots_view()})
    return tid


# ---------- App ----------
async def sessions_loop():
    """Every 5 s, read-only scan of your local interactive Claude Code sessions; push over SSE on change."""
    global SESSIONS
    while True:
        try:
            new = await asyncio.to_thread(sessions.scan)
            if new != SESSIONS:
                SESSIONS = new
                push({"type": "sessions", "sessions": SESSIONS})
        except Exception as e:   # 掃描失敗不能影響主服務
            print("sessions scan error:", e)
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    global TOKEN
    config.CACHE_DIR.mkdir(mode=0o700, exist_ok=True)
    TOKEN = secrets.token_urlsafe(32)
    tf = config.CACHE_DIR / "token"
    fd = os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(TOKEN)
    os.chmod(tf, 0o600)
    db.init()
    BOTS.update(botlib.load_bots())
    for b in BOTS.values():   # memory.md is git-ignored and created on first start
        mf = config.BOTS_DIR / b["id"] / "memory.md"
        if b["memory"] and not mf.exists():
            mf.write_text(f"# {b['id']} memory\n(The bot appends long-term notes here.)\n", encoding="utf-8")
    bg = [asyncio.create_task(sessions_loop())] if config.SESSIONS_ENABLED else []
    for b in BOTS.values():
        RT[b["id"]] = {"status": "idle", "action": "", "task_id": None}
        if not b["is_router"]:
            QUEUES[b["id"]] = asyncio.Queue()
            bg.append(asyncio.create_task(worker_loop(b["id"])))
    yield
    for p in list(PROCS.values()):      # 關機時殺掉所有 Bot 子行程(整個 process group)
        await runner.kill_group(p, 2)
    for w in bg:
        w.cancel()


app = FastAPI(title="cat-office-fleet", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    """防 DNS rebinding / CSRF:Host 與 Origin 白名單、/api/* 要 token、POST 一定要 application/json。"""
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "bad host"}, 403)
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "bad origin"}, 403)
    if request.url.path.startswith("/api/"):
        tok = request.headers.get("x-fleet-token") or request.query_params.get("token") or ""
        if not hmac.compare_digest(tok.encode("utf-8", "replace"), TOKEN.encode()):   # 非 ASCII 也不會 500
            return JSONResponse({"detail": "token required"}, 401)
        if request.method == "POST" and not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "Content-Type must be application/json"}, 415)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"                          # anti-clickjacking / framing
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"   # (no full CSP: the page uses inline scripts/styles)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


class TaskIn(BaseModel):
    text: str
    bot: str | None = None
    notify: bool = True        # false = no Telegram push (./fleet ask returns the answer directly)


@app.post("/api/tasks")
async def post_task(t: TaskIn):
    if not t.text.strip():
        raise HTTPException(400, "text 不能空白")
    tid = await submit(t.text.strip(), t.bot or None, t.notify)
    return db.q("SELECT * FROM tasks WHERE id=?", (tid,))[0]


@app.get("/api/bots")
def get_bots():
    return bots_view()


@app.get("/api/tasks")
def get_tasks(bot: str | None = None, limit: int = 50):
    limit = max(1, min(limit, 200))
    if bot:
        return db.q("SELECT * FROM tasks WHERE bot=? OR (route_by='router' AND ?='router') ORDER BY id DESC LIMIT ?",
                    (bot, bot, limit))
    return db.q("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/tasks/{tid}")
def get_task(tid: int):
    rows = db.q("SELECT * FROM tasks WHERE id=?", (tid,))
    if not rows:
        raise HTTPException(404)
    return rows[0]


@app.post("/api/bots/{bot_id}/rotate")
async def post_rotate(bot_id: str):
    """排進該 Bot 的佇列(等目前任務做完)→ 收尾 + 換新對話。"""
    if bot_id not in BOTS or BOTS[bot_id]["is_router"]:
        raise HTTPException(404)
    if not db.get_state(bot_id)["session_id"]:
        raise HTTPException(409, "目前沒有對話可收尾")
    tid = db.x("INSERT INTO tasks(text,bot,status,route_reason,route_by,created) VALUES(?,?,?,?,?,?)",
               ("[收尾並換新對話] 手動", bot_id, "queued", "手動", "system", time.time()))
    QUEUES[bot_id].put_nowait(("rotate", "手動", tid))
    push({"type": "bots", "bots": bots_view()})
    return {"ok": True, "queued": True, "task_id": tid}


@app.get("/api/tasks/{tid}/events")
def get_events(tid: int):
    rows = db.q("SELECT * FROM events WHERE task_id=? ORDER BY id", (tid,))
    for r in rows:
        r["data"] = json.loads(r["data"])
    return rows


@app.post("/api/tasks/{tid}/cancel")
async def cancel(tid: int):
    rows = db.q("SELECT status FROM tasks WHERE id=?", (tid,))
    if not rows:
        raise HTTPException(404)
    if rows[0]["status"] not in ("queued", "running"):
        raise HTTPException(409, f"狀態是 {rows[0]['status']},不能取消")
    CANCELLED.add(tid)                 # 先標記:run_task 在登記 proc 後會再檢查一次
    if tid in PROCS:
        asyncio.create_task(runner.kill_group(PROCS[tid]))
    elif rows[0]["status"] == "queued":   # 還在佇列:輪到時 run_task 會跳過
        CANCELLED.discard(tid)
        db.x("UPDATE tasks SET status='cancelled', finished=? WHERE id=?", (time.time(), tid))
        emit(tid, "?", "cancelled", {})
    return {"ok": True}


@app.post("/api/bots/{bot_id}/reset")
async def reset(bot_id: str, body: dict | None = None):
    """重置對話:忘掉 session_id,下一個任務開新 session(memory.md 保留)。"""
    if bot_id not in BOTS or BOTS[bot_id]["is_router"]:
        raise HTTPException(404)
    if RT[bot_id]["status"] in ("thinking", "working"):
        raise HTTPException(409, "Bot 正在執行,先取消任務")
    db.x("DELETE FROM bot_state WHERE bot=?", (bot_id,))
    if (body or {}).get("memory") and BOTS[bot_id]["memory"]:   # 選擇性連記憶一起清
        (config.BOTS_DIR / bot_id / "memory.md").write_text(f"# {bot_id} memory\n(The bot appends long-term notes here.)\n", encoding="utf-8")
    push({"type": "bots", "bots": bots_view()})
    return {"ok": True}


@app.get("/api/sessions")
def get_sessions():
    return SESSIONS


@app.get("/api/events")
async def sse():
    q = asyncio.Queue()
    SUBS.append(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'bots', 'bots': bots_view()}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'sessions', 'sessions': SESSIONS}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 15)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # 心跳,避免連線被中間層斷掉
        finally:
            SUBS.remove(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})



def _page(name: str) -> HTMLResponse:
    # The server injects the token into the page (readable same-origin only); the CLI reads .cache/token
    html = (config.ROOT / "static" / name).read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__FLEET_TOKEN__", TOKEN), headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def index():
    # The cat office (built from room/room-live-v7.tpl.html by room/build.py). ?demo=1 = fake demo data.
    return _page("index.html")
