# CLI entry (run through ./fleet).  `ask` exit codes: 0 ok / 1 task error / 2 could not route / 3 timeout / 4 cancelled or interrupted
import json, sys, time, urllib.request, urllib.error
from fleetapp import config
api = f"http://{config.HOST}:{config.PORT}"
try:
    token = (config.CACHE_DIR / "token").read_text().strip()
except OSError:
    sys.exit("token file not found: is the server running? (./fleet.sh start)")

def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    r = urllib.request.Request(api + path, data, {"Content-Type": "application/json", "X-Fleet-Token": token}, method=method)
    try:
        return json.load(urllib.request.urlopen(r, timeout=120))
    except urllib.error.HTTPError as e:
        sys.exit(f"Error {e.code}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach the server: {e.reason}")

a = sys.argv[1:]
if a[:1] == ["task"] and len(a) >= 2:
    t = call("/api/tasks", {"text": a[1], **({"bot": a[2]} if len(a) > 2 and a[2] else {})})
    print(f"Task #{t['id']} -> {t['bot']} ({t['route_reason']}) status {t['status']}")
elif a[:1] == ["bots"]:
    print(json.dumps(call("/api/bots"), ensure_ascii=False, indent=1))
elif a[:1] == ["tasks"]:
    print(json.dumps(call(f"/api/tasks?limit={a[1] if len(a) > 1 else 10}"), ensure_ascii=False, indent=1))
elif a[:1] == ["reset"] and len(a) > 1:
    print(call(f"/api/bots/{a[1]}/reset", {"memory": "--memory" in a}, "POST"))
elif a[:1] == ["rotate"] and len(a) > 1:
    bid = a[1]
    b0 = next((b for b in call("/api/bots") if b["id"] == bid), None)
    if not b0:
        sys.exit(f"No such bot: {bid}")
    print(f"Before wrap-up: session={b0['session_id']} context {b0['ctx_tokens']//1000}k memory {b0['memory_bytes']}")
    tid = call(f"/api/bots/{bid}/rotate", {}, "POST")["task_id"]
    for _ in range(450):   # up to 15 min: queued behind the current task + 5 min wrap-up cap
        time.sleep(2)
        t = call(f"/api/tasks/{tid}")
        if t["status"] not in ("queued", "running"):
            break
    else:
        sys.exit("Timed out; wrap-up is still running (check the dashboard)")
    evs = {e["kind"]: e["data"] for e in call(f"/api/tasks/{tid}/events")}
    if "rotated" in evs:
        d = evs["rotated"]
        w = "skipped (no memory)" if d["wrapup"] is None else "ok" if d["wrapup"] else "failed (forced fresh conversation; memory backed up)"
        print(f"Fresh conversation started; wrapup={w}; memory {d['memory_before']} -> {d['memory_after']} bytes")
    elif "rotate_failed" in evs:
        print(f"Wrap-up failed (wrapup=false): keeping the old conversation, will retry before the next task. {t.get('result') or ''}", file=sys.stderr); sys.exit(1)
    else:
        print(t.get("result") or t["status"], file=sys.stderr); sys.exit(0 if t["status"] == "done" else 1)
elif a[:1] == ["ask"]:
    args, timeout, i = [], 900, 1
    while i < len(a):
        if a[i] == "--timeout":
            try:
                timeout = int(a[i + 1]); assert timeout > 0
            except (IndexError, ValueError, AssertionError):
                sys.exit("--timeout needs a positive integer (seconds)")
            i += 2
        else:
            args.append(a[i]); i += 1
    if not args:
        sys.exit('usage: ./fleet ask "<task>"|- [bot-id] [--timeout SECONDS]')
    text = sys.stdin.read() if args[0] == "-" else args[0]
    bot_id = args[1] if len(args) > 1 else None
    if not text.strip():
        sys.exit("task text is empty")
    t = call("/api/tasks", {"text": text, "notify": False, **({"bot": bot_id} if bot_id else {})})   # ask returns the answer itself; no Telegram push
    tid = t["id"]
    t_sub, t_run, fails = time.time(), None, 0
    def cancel(msg, code):
        try: call(f"/api/tasks/{tid}/cancel", {}, "POST")
        except SystemExit: pass
        print(f"[#{tid} {msg}, cancelled]", file=sys.stderr); sys.exit(code)
    try:
        while t["status"] in ("routing", "queued", "running"):
            now = time.time()
            if t["status"] == "running" and t_run is None:
                t_run = now           
            if t_run is not None and now - t_run > timeout:
                cancel(f"timeout {timeout}s", 3)
            if t_run is None and now - t_sub > timeout + 900:   # separate cap for queueing / wrap-up
                cancel("queue timeout", 3)
            time.sleep(1.5)
            try:
                t = call(f"/api/tasks/{tid}"); fails = 0
            except SystemExit as e:   # connection errors: tolerate a few, then cancel
                fails += 1
                if fails >= 5:
                    cancel(f"connection lost ({e})", 4)
    except KeyboardInterrupt:
        cancel("interrupted", 4)
    tok = t["input_tokens"] + t["output_tokens"] + t["cache_create"]
    print(f"[#{tid} bot={t['bot']} status={t['status']} tokens={tok} {(t.get('duration_ms') or 0)/1000:.1f}s]", file=sys.stderr)
    if t["status"] == "unassigned":
        print(f"The router could not dispatch: {t['route_reason']}", file=sys.stderr); sys.exit(2)
    if t["status"] == "cancelled":
        sys.exit(4)
    if t["status"] != "done":
        print(t.get("result") or t["status"], file=sys.stderr); sys.exit(1)
    print(t["result"])
else:
    print('usage: ./fleet task "..." [bot-id] | bots | tasks [n] | reset <bot-id> [--memory] | rotate <bot-id> | ask "..."|- [bot-id] [--timeout SECONDS]')
