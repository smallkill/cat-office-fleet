# AGENTS.md: operating guide for AI agents working in this repo

Cat Office Fleet = FastAPI server + one-page canvas UI that dispatches tasks to Claude Code bots (`claude -p`) and
draws each bot as a cat. Runs on the user's Claude Code subscription. Read this whole file before changing anything.

## Architecture map

| Path | What it is |
|---|---|
| `fleetapp/config.py` | all settings (env vars / `.env`); loopback-only host check |
| `fleetapp/main.py` | FastAPI app: API, SSE, per-bot queues, task runner, rotation, optional Telegram push, security middleware (`guard`) |
| `fleetapp/runner.py` | the ONLY place that builds `claude` commands and the child environment (`clean_env`); router call; process-group kill |
| `fleetapp/bots.py` | parses `bots/<id>/BOT.md` |
| `fleetapp/db.py` | SQLite (`data/fleet.db`): `bot_state`, `tasks`, `events` |
| `fleetapp/sessions.py` | optional (`FLEET_SESSIONS=1`) read-only scan of local interactive Claude Code sessions |
| `fleetapp/cli.py` | `./fleet` CLI (task / ask / bots / tasks / rotate / reset) |
| `bots/<id>/BOT.md`, `bots/<id>/.claude/settings.json` | a bot: definition + per-bot deny list. `bots/<id>/memory.md` is generated and git-ignored |
| `room/room-live-v7.tpl.html` | **source** of the whole UI (canvas drawing, cats, rooms, task bar, chat panel). One placeholder `__FLEET_TOKEN__` |
| `room/build.py` | copies the template to `static/index.html` (the served page). Run `python3 room/build.py` after every template edit and commit both |
| `fleet.sh` | start / stop / status / logs (background uvicorn on loopback) |

Flow: `POST /api/tasks` -> router (haiku, no tools) unless a bot is named -> per-bot `asyncio.Queue` -> `runner.worker_cmd`
spawns `claude -p ... --resume <sid>` in `bots/<id>/` -> stream-json lines become events (SQLite + SSE) -> UI.

## BOT.md front matter

```
---
name: Display name          # shown in the UI (a trailing " Bot" is trimmed)
emoji: "📰"
builtin: WebSearch, WebFetch  # --tools: the only built-in tools the model can see
allow: WebSearch, WebFetch    # --allowedTools: allowed without asking; Bash rules = exact full commands
add_dirs: /abs/a, /abs/b      # --add-dir: extra readable dirs (absolute, comma separated)
memory: false                 # true: memory.md is fed to the system prompt and Edit(memory.md) is auto-allowed
router: true                  # only for the router bot (exactly one, id "router")
---
## Role (the router dispatches based on this section)   <- "## Role" or "## 職責"; the router reads exactly this text
## Rules
```

- `builtin` = visibility. `allow` = no-prompt permission. A tool in `builtin` but not in `allow` is denied (`dontAsk`).
- A `memory: true` bot needs `Read, Edit` in `builtin`; the server adds the `Edit(//abs/path/memory.md)` allow rule itself.
- Adding or editing a bot: change `bots/<id>/`, copy `.claude/settings.json` from `bots/news`, restart (`./fleet.sh stop && ./fleet.sh start`). Template for a file-access bot: `docs/bot-template.md`.
- Bot ids are folder names; the cat coat comes from `BOTCOAT` in the template (else a hash of the id).

## Safety rules (do not break these)

1. **Never broaden permissions silently.** Any change to `allow`, `builtin`, `add_dirs`, a `settings.json` deny list, `runner.py` flags, or the `guard` middleware must be called out to the user explicitly, with the reason.
2. **Never put bare `Read`, `Grep`, `Glob`, `Bash`, `Edit` or `Write` in `allow`.** Bare rules allow any path or command. Use exact `Bash(<full command>)` strings; reads are covered by cwd + `add_dirs`.
3. **Never expose the server beyond 127.0.0.1.** Do not change the host check in `config.py`, do not add `0.0.0.0`, tunnels or CORS, do not weaken the Host/Origin/token checks. There is no login system.
4. **Never set or pass `ANTHROPIC_API_KEY`** (or other API/cloud-provider variables) to child processes; `runner.clean_env` strips them on purpose. The project must stay on subscription billing.
5. **Never commit secrets or data:** `.env`, `.cache/`, `data/*.db`, `bots/*/memory.md`, tokens, chat ids. Check `git status` before committing.
6. Do not remove `--setting-sources project`, `--strict-mcp-config`, `--permission-mode dontAsk`, or the per-bot deny lists.
7. Keep Telegram push optional (off unless both env vars are set), async, time-limited and non-fatal; never notify for `ask` tasks.
8. UI text from the server (task text, bot output, session names) must go in with `textContent`, never `innerHTML`.

## Customizing the cats (all inside `room/room-live-v7.tpl.html`)

- Coats: `const COATS={...}` (29 entries; types `tabby/solid/tux/calico/siamese`, colours, optional `pat` patches). Add a coat there; `?coats=1` shows all coats.
- Which bot gets which coat: `BOTCOAT={router:"ginger",...}`. Unlisted bots get `COATKEYS[hash(id)]`.
- Rooms and furniture: `const ROOMS={...}`, the `SLOT` table (where cats rest/work), `SOFA/WTABS/BOXES/PADS/POUFS/SPOTS` constants and the drawing functions above them.
- Poses: `POSE_ORDER` and the pose drawing code; idle behaviour (lazy timings) around `LZ`/`LD`/`REST_OK`; `setState(id, mode, opt)` maps a status to an animation.
- Status -> animation mapping for real bots: `botMode()` (and `TOOLKIND()` for tool icons) near the bottom.
- Demo mode (`?demo=1`): `DEMO_CATS` and `SCRIPT` (64 s loop). Use it for screenshots.
- After any edit: `python3 room/build.py`, restart not required (page is read per request), hard-reload the browser.

## Build, run, test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python3 room/build.py                       # template -> static/index.html
./fleet.sh start            # default port 8930 (set FLEET_PORT to use another); ./fleet.sh stop afterwards
./fleet ask "Reply with OK" news      # tiny end-to-end test (uses subscription quota; keep tasks tiny)
./fleet task "..."          # exercises the router
```

There is no unit-test suite. Verify UI changes in a browser with `http://127.0.0.1:<port>/?demo=1` and check the console
for errors (Playwright: `pageerror` / console errors must be empty). `window.ROOM` exposes `cats`, `state()`, `setState()` for scripted checks.

## Common pitfalls

- Editing `static/index.html` directly: it is generated; edit the template and rebuild.
- The template must contain exactly one `__FLEET_TOKEN__` (build asserts it). The token changes at every server start; reload the page.
- Permission rule paths: absolute paths need a double slash (`Edit(//abs/path)`), `~/` means home. A single leading slash is project-relative.
- `claude -p` with `ANTHROPIC_API_KEY` set silently switches to API billing; never do that.
- Changing `BOT.md` needs a server restart (bots are loaded at startup).
- Compound Bash commands are split by Claude Code and each part is checked; exact-command `allow` rules plus the deny lists are what keep Bash safe. Do not "simplify" them.
- Task text starts with `-`? It goes through stdin on purpose; do not move it to argv.
- Long conversations are rotated (`FLEET_CTX_LIMIT`, 7 days). Bots without memory skip the wrap-up and just start fresh.
- macOS has no `/proc`: descendant cleanup and session cats are Linux/WSL only.
