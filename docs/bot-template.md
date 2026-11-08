# Bot template: a bot that can read files and run one exact command

Copy this into `bots/<id>/BOT.md` (the folder name is the bot id), copy `bots/news/.claude/settings.json` to
`bots/<id>/.claude/settings.json`, then restart (`./fleet.sh stop && ./fleet.sh start`).

```markdown
---
name: Notes Bot
emoji: "📓"
# builtin = the ONLY built-in tools the model can see at all (claude --tools).
builtin: Read, Grep, Glob, Bash, Edit
# allow = rules that run WITHOUT asking (claude --allowedTools). Everything else is denied (dontAsk).
# Bash rules must be the exact full command. Do not add bare Read / Grep / Glob / Bash here: a bare
# rule would allow any path / any command. Reads inside the bot's own folder and inside add_dirs
# are allowed automatically.
allow: Bash(/path/to/notes/.venv/bin/python /path/to/notes/summarize.py)
# add_dirs = extra directories the bot may read (claude --add-dir). Comma separated, absolute paths.
add_dirs: /path/to/notes, /path/to/Documents/journal
# memory: true = memory.md is fed back into the system prompt and the bot may Edit ONLY that file.
memory: true
---
## Role (the router dispatches based on this section)
Answers questions about my notes under ~/notes and the journal under ~/Documents/journal.

## Rules
- Read-only outside your own memory.md. Never send anything outside this machine.
- To refresh the summary, run exactly:
  `/path/to/notes/.venv/bin/python /path/to/notes/summarize.py`
  (full absolute path, no arguments, no `cd`, no pipes).
- Do not paste large raw excerpts of private files; summarise.
```

Checklist before you save it:

1. Every path is absolute and belongs to this bot's purpose; no `~`, no `/`, no your whole home directory.
2. `allow` contains only `WebSearch` / `WebFetch` (if needed) and exact `Bash(...)` commands. No bare tool names.
3. The script you allow is read-only (or its side effects are acceptable), takes no user-controlled arguments.
4. `bots/<id>/.claude/settings.json` exists with the deny list (copy from `bots/news`), and add your own
   sensitive folders to it, e.g. `"Read(~/private/**)"`.
5. If the bot has `WebFetch`, keep the loopback denies from `bots/news` (`WebFetch(domain:127.0.0.1)`, `localhost`, `[::1]`, `0.0.0.0`) so it cannot call the local API.
6. Test with a harmless task via `./fleet ask "..." <id>` and check the timeline for denied tool calls.
