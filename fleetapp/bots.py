"""讀取 bots/<id>/BOT.md。

BOT.md = front matter(key: value)+ Markdown 內文。欄位:
  name / emoji
  builtin: 這個 Bot 可用的內建工具(--tools);沒列的工具模型根本看不到
  allow:   免詢問自動放行的規則(--allowedTools),Bash 規則必須是「完整指令」
  add_dirs: 額外可讀目錄(--add-dir);cwd(bots/<id>)與 add_dirs 內的讀取是自動允許的
  memory:  true 才會把 memory.md 餵給 Bot 並開放它 Edit
新增 Bot 只要新增資料夾與 BOT.md,重啟服務即可(見 README)。
"""
import re

from .config import BOTS_DIR, MEM_CAP   # MEM_CAP:餵給 Bot 的 memory.md 上限(取尾端)


def _split(text):
    text = text.replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, m.group(2).strip()


def _split_list(s):
    """以逗號分隔,但括號內的逗號/空白不切(Bash(...) 規則)。"""
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def load_bots():
    bots = {}
    for d in sorted(BOTS_DIR.iterdir()):
        f = d / "BOT.md"
        if not f.is_file():
            continue
        meta, body = _split(f.read_text(encoding="utf-8"))
        bots[d.name] = {
            "id": d.name,
            "name": meta.get("name", d.name),
            "emoji": meta.get("emoji", "🤖"),
            "is_router": meta.get("router") == "true",
            "builtin": _split_list(meta.get("builtin", "")),
            "allow": _split_list(meta.get("allow", "")),
            "add_dirs": [p.strip() for p in meta.get("add_dirs", "").split(",") if p.strip()],
            "memory": meta.get("memory") == "true",
            "body": body,
            "dir": str(d),
            "description": _job(body),   # the "## Role" section: read by the router and shown in the UI
        }
    return bots


def _job(body):
    m = re.search(r"## (?:Role|職責)[^\n]*\n(.*?)(?=\n## |\Z)", body, re.S)
    return (m.group(1) if m else body).strip()


def memory_bytes(bot):
    f = BOTS_DIR / bot["id"] / "memory.md"
    return f.stat().st_size if bot["memory"] and f.exists() else 0


def memory_text(bot):
    if not bot["memory"]:
        return ""
    f = BOTS_DIR / bot["id"] / "memory.md"
    if not f.exists():
        return ""
    t = f.read_text(encoding="utf-8")
    return t if len(t.encode()) <= MEM_CAP else "(…前面較舊的內容已省略)\n" + t.encode()[-MEM_CAP:].decode(errors="ignore")
