# Hopper

**A durable priority job queue for AI agents.** Drop work in from anywhere (a chat bot, a
cron job, a person at a shell, another agent); any agent harness takes it out, does it, and
reports back with evidence. One SQLite file, zero dependencies, runs on a Raspberry Pi.

```sh
hop add "Find out why the nightly build fails" -p high -d "root cause + a fix PR"
hop work --caps shell,git --exec "claude -p --allowedTools 'Bash(hop:*),Read,Edit,Grep'"
```

## Why

Agents are good at doing work and bad at *holding* it. Context windows end, processes
crash, two agents pick up the same task, a "done" turns out to be a guess, and urgent requests
wait behind long background jobs. Hopper keeps the work outside the agent:

- **Durable.** Jobs live in SQLite, not in a context window. Crash mid-job and the lease
  expires; the job goes back to the queue (or is failed after its last attempt).
- **One owner at a time.** Claims are atomic and leased. Tested with many workers racing
  over one file: every job is claimed exactly once.
- **The person waiting comes first.** Strict priority, reserved worker slots for urgent
  work, and aging so low-priority jobs are never starved.
- **Done means done, with evidence.** Completing a job records a summary *and* what was
  checked. A queue can refuse completions without evidence.
- **Built for delegation.** A worker can split a job into child jobs (each with the
  capabilities it needs, in order where it matters); the parent comes back with every
  child's result. A worker can ask the requester a question and move on while it waits.
- **Results go back where the work came from:** Telegram, a webhook, a file, or a hook
  command, through a retrying outbox.
- **No duplicates.** An idempotency key makes re-adding the same job return the open one.
- **Harness-agnostic.** CLI, MCP server, HTTP API and Python library over the same queue.
  Works with Claude Code, Hermes, OpenClaw, or anything that can run a command.

## Install

Python 3.11+ and nothing else.

```sh
pipx install git+https://github.com/Humdan/hopper      # or: pip install git+https://...
hop init                                               # database + commented sample config
```

## The 60-second tour

```sh
# hand work off
hop add "Summarize yesterday's error logs" -b "Logs are in /var/log/app/" \
        -d "Top 3 errors with counts" -p high --reply-to file:results.jsonl

# see the queue: running first, then what's next
hop ls

# take the most important job this worker can do, and read its brief
hop claim --caps shell --brief

# finish it, with evidence
hop done j_01J... -m "3 errors: A (41), B (12), C (3)" -e "grep -c over app.log: ..."

# or: ask, split, release, fail
hop ask j_01J... "Only production logs, or staging too?"
hop split j_01J... --child "Collect logs :: ..." --child "Analyze :: ..."
```

`hop <command> --help` documents every option.

## Concepts

| | |
|---|---|
| **Job** | title, instructions, `done_when`, priority 0–100 (`urgent` 100, `high` 70, `normal` 50, `low` 20, `background` 10, `idle` 5), queue, required capabilities, tags, `reply_to`, idempotency key, dependencies, earliest start, attempts, lease timeout |
| **States** | `queued` → `running` → `done`; also `needs_input` (asked a question), `waiting` (split into children), `failed`, `cancelled` |
| **Claim** | the best job a worker can do: highest effective priority, then oldest, with every required capability, dependencies done, start time reached |
| **Lease** | a claim expires without a heartbeat; the job returns to the queue |
| **Brief** | the prompt a worker gets: goal, done-when, the question/answer thread, sub-job results, earlier failed attempts, and how to report back |

The full model is in [docs/design.md](docs/design.md).

## Interfaces

| | |
|---|---|
| `hop` | the CLI above |
| `hop mcp` | MCP server on stdio: `hop_add`, `hop_claim`, `hop_complete`, `hop_split`, `hop_ask`, … |
| `hop serve` | HTTP API with scoped bearer tokens (`read`, `produce`, `work`, `admin`) |
| `hop work` | runs any agent command as a worker: slots, reserved urgent slots, heartbeats, timeouts |
| `from hopper import Hopper` | the Python library |

Set `HOPPER_URL` and `HOPPER_TOKEN` and the CLI, MCP server and workers talk to a remote
`hop serve` instead of a local file, so many machines can share one queue.

## Chat apps, media and the relay

Optional, off unless configured ([docs/channels.md](docs/channels.md)):

- **Channels:** Telegram and WhatsApp behind one interface with allowlists.
  `hop send telegram:<chat> "text" --file chart.png`, and jobs can reply to a chat.
- **Media job types:** `transcribe`, `speak`, `describe` and `image` run through
  `hop work --builtin media` with an OpenRouter key: `hop add --type image "a fox at dusk"`.
- **`hop relay`:** talk to Claude Code from your phone. A tiny always-on process keeps
  Claude warm while you chat (about a second per reply) and lets it exit when you stop;
  permission prompts come to the chat as yes/no, voice notes are transcribed, and a
  fallback model answers if Claude can't. For your own use on your own machine.

## Integrations

- [Claude Code](integrations/claude-code/README.md)
- [Hermes Agent](integrations/hermes/README.md)
- [OpenClaw](integrations/openclaw/README.md)
- [A skill that teaches any agent the protocol](integrations/SKILL.md)
- [systemd units](examples/systemd/)

## Security

- `hop serve` binds `127.0.0.1` by default and refuses to run unauthenticated on any other
  address. Tokens are scoped, and a token can only act as its own workers.
- A job's `reply_to` can't make Hopper do arbitrary things: Telegram chats and webhook URLs
  must be allow-listed, files stay inside one directory, and hooks are commands named in
  *your* config, never in the job.
- Secrets never go in the config file: write `secret = "env:NAME"` and keep the value in the
  environment.

## Status

v0.2 adds channels (Telegram, WhatsApp), media job types and `hop relay`. v0.1: the core model, CLI, MCP, HTTP and runner are complete and tested (`python -m
unittest discover -s tests -t .`). Next: a Postgres backend for large deployments, a web
view, recurring jobs.

MIT licensed.
