#!/usr/bin/env bash
# Usage: ./fleet.sh {start|stop|status|logs}
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "No .venv yet. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
eval "$(.venv/bin/python -c 'from fleetapp import config as c; print(f"PORT={c.PORT} HOST={c.HOST} CACHE={c.CACHE_DIR}")')"
PID=$CACHE/fleet.pid; LOG=$CACHE/fleet.log
mkdir -p -m 700 "$CACHE"
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
case "$1" in
  start)
    running && { echo "Already running (pid $(cat "$PID"))"; exit 0; }
    unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN      # safety: stay on subscription billing, never the API
    umask 077; : > "$LOG"; chmod 600 "$LOG"           # token is regenerated at every start; log readable by you only
    nohup .venv/bin/uvicorn fleetapp.main:app --host "$HOST" --port "$PORT" --timeout-graceful-shutdown 5 --no-access-log >> "$LOG" 2>&1 &
    echo $! > "$PID"; sleep 1.5
    running && echo "Started pid $(cat "$PID") -> http://$HOST:$PORT" || { echo "Failed to start, see ./fleet.sh logs"; tail -20 "$LOG"; exit 1; } ;;
  stop)
    if running; then
      p=$(cat "$PID"); kill "$p"
      for i in $(seq 1 100); do kill -0 "$p" 2>/dev/null || break; sleep 0.1; done   # wait up to 10 s
      kill -0 "$p" 2>/dev/null && { kill -9 "$p"; echo "Timed out, sent kill -9"; }
      rm -f "$PID"; echo "Stopped"
    else echo "Not running"; fi ;;
  status)
    running && { echo "Running (pid $(cat "$PID"))"; curl -s -m 3 -H "X-Fleet-Token: $(cat "$CACHE/token")" "http://$HOST:$PORT/api/bots" | head -c 200; echo; } || echo "Not running" ;;
  logs) tail -n 50 -f "$LOG" ;;
  *) echo "Usage: $0 {start|stop|status|logs}"; exit 1 ;;
esac
