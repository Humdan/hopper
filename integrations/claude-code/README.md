# Hopper with Claude Code

Two roles: Claude Code can **use** the queue during a session (hand work off, check on it),
and it can **be a worker** that takes jobs from the queue unattended.

## 1. Tools in every session (MCP)

```sh
claude mcp add hopper -- hop mcp
```

Claude now has `hop_add`, `hop_claim`, `hop_complete`, `hop_split` and the rest. To reach a
Hopper on another machine instead of the local database:

```sh
claude mcp add hopper -e HOPPER_URL=https://queue.example.com -e HOPPER_TOKEN=... -- hop mcp
```

## 2. Teach it the protocol (skill)

```sh
mkdir -p ~/.claude/skills/hopper
cp integrations/SKILL.md ~/.claude/skills/hopper/SKILL.md
```

## 3. Claude Code as a worker

```sh
hop work --name claude --caps shell,git,web --slots 2 --reserve 1 \
  --exec "claude -p --output-format json --allowedTools 'Bash(hop:*),Read,Edit,Write,Grep,Glob'"
```

`hop work` pipes each job's brief to `claude -p` on stdin, keeps the claim alive while it
runs, records the run's cost, tokens and time from the JSON result (see `hop report`), and sets `HOPPER_JOB_ID` / `HOPPER_WORKER` so `hop done` inside the session needs no
extra flags. `--reserve 1` keeps one slot free for `high`/`urgent` jobs.

Choose the tool permissions deliberately: headless runs can't ask you to approve anything, so
list what the worker may use. It needs at least `Bash(hop:*)` to report back.

## 4. Show your sessions on the dashboard (presence)

Interactive sessions never claim a job, so by default they are invisible. `hop-pulse` is a hook
that reports each session (and what it is doing now) to Hopper:

```sh
HOPPER_URL=https://queue.example.com HOPPER_TOKEN=... sh integrations/claude-code/install-hop-pulse.sh
```

That installs the hook, writes `~/.config/hopper/pulse.env` (mode 600) and registers it. To do it by hand,
copy `hop-pulse` to `~/.local/bin/`, write `pulse.env` with those two variables, then register it in `~/.claude/settings.json` for `SessionStart`, `UserPromptSubmit`,
`PostToolUse` (matcher `*`), `Stop` and `SessionEnd`, each as
`{"type": "command", "command": "~/.local/bin/hop-pulse", "timeout": 5}`. A `work` token is enough.
Sessions appear under "Connected workers" with their current action, and prompts show in Live
activity. The hook is silent on failure and throttles tool pings to one per 20 s. Any script can do the
same with `hop ping "what I'm on" --pulse` (or `POST /v1/ping`).
