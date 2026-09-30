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
  --exec "claude -p --allowedTools 'Bash(hop:*),Read,Edit,Write,Grep,Glob'"
```

`hop work` pipes each job's brief to `claude -p` on stdin, keeps the claim alive while it
runs, and sets `HOPPER_JOB_ID` / `HOPPER_WORKER` so `hop done` inside the session needs no
extra flags. `--reserve 1` keeps one slot free for `high`/`urgent` jobs.

Choose the tool permissions deliberately: headless runs can't ask you to approve anything, so
list what the worker may use. It needs at least `Bash(hop:*)` to report back.
