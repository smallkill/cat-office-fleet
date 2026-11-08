"""呼叫 `claude -p` 的唯一入口(worker Bot 與 router 都從這裡出去)。

訂閱計費的關鍵:子行程環境一定要剝掉 ANTHROPIC_API_KEY 等變數,
否則 claude -p 會改走 API 計費。clean_env() 是唯一建環境的地方。
"""
import asyncio
import json
import os
import re
import signal

from . import config

# 這些變數一旦存在,claude 會改用 API/第三方計費或認證 → 一律剝掉
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    if "ANTHROPIC_API_KEY" in env:
        raise RuntimeError("ANTHROPIC_API_KEY 不該出現在子行程環境")
    env["FLEET_BOT"] = "1"                          # 讓其他腳本/guard 能辨識「這是 Bot 在跑」
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"    # do not write ~/.claude/projects/*/memory
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"     # do not load your ~/.claude/CLAUDE.md etc.
    return env


# 共用旗標:只讀 bots/<id>/.claude/settings.json(project),不載入使用者全域 hooks/設定、MCP、skills。
# 登入認證(OAuth)不受影響。
BASE_FLAGS = ["--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands",
              "--settings", '{"autoMemoryEnabled": false}']


def worker_cmd(bot, session_id, memory):
    """組指令。任務文字不放在 argv(開頭是 '-' 會被當選項),改由 stdin 傳入。"""
    allowed = list(bot["allow"])
    if bot["memory"]:
        mem_file = f"//{bot['dir'].lstrip('/')}/memory.md"   # 權限規則的絕對路徑要雙斜線
        allowed.append(f"Edit({mem_file})")
    system = f"{bot['body']}\n\n## 你的長期記憶 (memory.md)\n{memory}" if bot["memory"] else bot["body"]
    # 系統提示寫進 0600 檔再用 -file 傳:argv 會被 `ps aux` 看到(含 memory.md),也避免 E2BIG
    pdir = config.CACHE_DIR / "prompts"
    pdir.mkdir(mode=0o700, exist_ok=True)
    pf = pdir / f"{bot['id']}.md"
    fd = os.open(pf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(system)
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--append-system-prompt-file", str(pf),
           "--tools", ",".join(bot["builtin"]),          # 模型只看得到這些內建工具
           *(["--allowedTools", *allowed] if allowed else []),   # no-prompt rules; everything else is denied
           "--permission-mode", "dontAsk", *BASE_FLAGS]
    for d in bot["add_dirs"]:
        cmd += ["--add-dir", d]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


async def spawn(cmd, cwd, stdin_text, **kw):
    """啟動子行程:獨立 process group(取消時整組殺),任務文字走 stdin。"""
    p = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=clean_env(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, **kw)
    p.stdin.write(stdin_text.encode())
    await p.stdin.drain()
    p.stdin.close()
    return p


def descendants(pid):
    """走 /proc 找所有子孫行程(Bash 工具的子行程可能自己開 session,killpg 殺不到)。"""
    kids = {}
    try:
        names = os.listdir("/proc")
    except OSError:   # no /proc (macOS): process-group kill still works, descendants are best-effort
        return []
    for d in names:
        if d.isdigit():
            try:
                ppid = int(open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()[1])
                kids.setdefault(ppid, []).append(int(d))
            except (OSError, IndexError, ValueError):
                pass
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c); todo.append(c)
    return out


def _kill_pids(pids, sig):
    for x in pids:
        try:
            os.kill(x, sig)
        except OSError:
            pass


async def kill_group(p, grace=5):
    """先記下子孫 → SIGTERM 整組 → 等 grace 秒 → SIGKILL(含子孫)→ wait() 避免殭屍。"""
    if p.returncode is not None:
        return
    kids = descendants(p.pid)
    _kill_pids(kids, signal.SIGTERM)
    try:
        os.killpg(p.pid, signal.SIGTERM)
        await asyncio.wait_for(p.wait(), grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await p.wait()
    except ProcessLookupError:
        pass
    _kill_pids(kids, signal.SIGKILL)   # 還活著的子孫補一刀(已死的 pid 會 ESRCH,忽略)


def short(name, inp):
    """工具輸入的一行摘要,給儀表板顯示「現在在做什麼」。"""
    inp = inp or {}
    for key in ("command", "file_path", "path", "pattern", "query", "url"):
        if key in inp:
            v = str(inp[key]).replace("\n", " ")
            if key == "command":   # 只留腳本檔名與參數,路徑太長沒意義
                v = re.sub(r"/\S*/(\S+\.py)", r"\1", v)
            return v[:100]
    return json.dumps(inp, ensure_ascii=False)[:100]


async def run_router(task_text, bots):
    """幕僚長:一次性 haiku 呼叫,無工具,回 (bot_id, reason, raw_result)。"""
    workers = {k: b for k, b in bots.items() if not b["is_router"]}
    roster = "\n".join(f"- id={k} ({b['name']}): {b['description']}" for k, b in workers.items())
    prompt = (f"You are the chief of staff (router). Dispatch the task to the most suitable bot.\nBots:\n{roster}\n\nTask: {task_text}\n\n"
              'Output exactly one line of JSON and nothing else: {"bot": "<id or unassigned>", "reason": "<one short sentence, in the language of the task>"}. '
              "If the task fits no bot's role or you are unsure, use unassigned.")
    cmd = ["claude", "-p", "--model", config.ROUTER_MODEL, "--output-format", "json",
           "--tools", "", *BASE_FLAGS]
    try:
        p = await spawn(cmd, config.CACHE_DIR, prompt)
        try:
            out, err = await asyncio.wait_for(p.communicate(), config.ROUTER_TIMEOUT)
        except asyncio.TimeoutError:
            await kill_group(p, 2)
            raise RuntimeError("router timeout")
        if p.returncode != 0:
            raise RuntimeError(err.decode()[:200] or f"exit {p.returncode}")
        res = json.loads(out.decode())
        m = re.search(r"\{.*\}", res.get("result", ""), re.S)
        d = json.loads(m.group(0))
        if d.get("bot") not in workers:
            return "unassigned", d.get("reason", "could not decide"), res
        return d["bot"], d.get("reason", ""), res
    except Exception as e:  # 任何失敗都退回 unassigned,由使用者手動指定
        return "unassigned", f"router failed: {e}", {}
