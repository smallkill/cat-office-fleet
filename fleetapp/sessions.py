"""Optional feature (FLEET_SESSIONS=1): read-only scan of your running interactive Claude Code sessions.

Sources (read-only; never written to, no sockets opened). Only local metadata is read: status/cwd/name and
the last tool call + token count from the tail of the transcript. Nothing leaves your machine.
  ~/.claude/sessions/<pid>.json         → 狀態、cwd、名稱
  ~/.claude/projects/*/<sessionId>.jsonl → 只讀檔尾 200KB,取最後一個工具動作與今日 token
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import BOTS_DIR

SESS_DIR = Path.home() / ".claude" / "sessions"
PROJ_DIR = Path.home() / ".claude" / "projects"
TAIL = 200 * 1024
CLK = os.sysconf("SC_CLK_TCK")


def _starttime(pid):
    """/proc/<pid>/stat 第 22 欄(clock ticks);行程不存在回 None。comm 可能含空白,從最後一個 ')' 之後切。"""
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _is_fleet_child(pid, cwd):
    if cwd.startswith(str(BOTS_DIR)):
        return True
    try:   # 車隊子行程環境有 FLEET_BOT=1
        return b"FLEET_BOT=1" in open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    except OSError:
        return False


def _short(s, n=80):
    return re.sub(r"\s+", " ", str(s))[:n]


def _tool_summary(name, inp):
    inp = inp or {}
    for k in ("command", "file_path", "path", "pattern", "query", "url", "description"):
        if k in inp:
            return _short(inp[k])
    return ""


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _transcript(sid):
    if not isinstance(sid, str) or not UUID.fullmatch(sid):   # 擋路徑穿越
        return None
    for f in PROJ_DIR.glob(f"*/{sid}.jsonl"):
        return f
    return None


def _tail_info(sid):
    f = _transcript(sid)
    if not f:
        return None
    try:
        size = f.stat().st_size
        with open(f, "rb") as fh:          # 唯讀、只讀尾端
            fh.seek(max(0, size - TAIL))
            data = fh.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if size > TAIL:
        lines = lines[1:]                   # 第一行可能被截斷
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    last_tool, seen, tokens = None, set(), 0
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        msg = e.get("message", {})
        for blk in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if blk.get("type") == "tool_use":
                last_tool = f"{blk.get('name')}: {_tool_summary(blk.get('name'), blk.get('input'))}"
        mid, u = msg.get("id"), msg.get("usage") or {}
        try:
            ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts >= midnight and mid and mid not in seen:    # 同一則訊息會拆成多行,依 id 去重
            seen.add(mid)
            tokens += u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return {"last_action": last_tool, "tokens_today": tokens, "partial": size > TAIL}


def scan():
    now = time.time()
    out = []
    for f in SESS_DIR.glob("*.json"):
        if not re.fullmatch(r"\d+\.json", f.name):     # 略過 *.key 等檔
            continue
        try:
            out.append(_one(f, now))
        except Exception:   # 單一 session 壞掉不影響其他
            continue
    out = [x for x in out if x]
    out.sort(key=lambda s: (s["updated_ago"] is None, s["updated_ago"] or 0))
    return out


def _one(f, now):
    d = json.loads(f.read_text())
    pid, cwd = d.get("pid"), d.get("cwd", "")
    if not isinstance(pid, int) or not isinstance(cwd, str) or not cwd.startswith("/"):
        return None
    st = _starttime(pid)
    if st is None or (d.get("procStart") and str(d["procStart"]) != st):   # 已結束或 pid 被重用
        return None
    if _is_fleet_child(pid, cwd):
        return None
    info = _tail_info(d.get("sessionId", "")) or {}
    return {
        "pid": pid, "session_id": d.get("sessionId"),
        "name": d.get("name") or os.path.basename(cwd.rstrip("/")) or cwd,
        "cwd": cwd, "kind": d.get("kind"), "entrypoint": d.get("entrypoint"),
        "status": d.get("status") or "unknown",
        "updated_ago": int(now - d["updatedAt"] / 1000) if d.get("updatedAt") else None,
        "uptime": int(now - d["startedAt"] / 1000) if d.get("startedAt") else None,
        "last_action": info.get("last_action"), "tokens_today": info.get("tokens_today"),
        "tokens_partial": info.get("partial"),
    }
