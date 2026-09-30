# Hopper design

Hopper is a durable, priority job queue for AI agents. Anything can drop work in (a chat
bot, a cron job, a person at a shell, another agent), and any agent harness can take work
out, do it, and report back. It is one SQLite file and zero dependencies, so it runs on a
Raspberry Pi as well as a server, and it can be shared across machines over HTTP.

This document is the contract. The code follows it.

## Principles

1. **The queue is the source of truth.** A job's state lives in Hopper, not in an agent's
   context window. If an agent crashes, the job is still there.
2. **One owner at a time.** Claiming is atomic and time-limited (a lease). A job is never
   worked twice at once, and never lost when a worker dies.
3. **The person waiting comes first.** Priority is strict, with a reserved slot for urgent
   work so it never waits behind long background jobs. Old low-priority work ages upward so
   it is never starved.
4. **Done means done, with evidence.** Completing a job records what was done and how it
   was verified. Finished jobs leave the queue; their history stays until it is trimmed.
5. **Results go back where the work came from.** Every job can carry a `reply_to`
   address, and Hopper delivers the outcome there.
6. **Harness-agnostic.** The same queue is reachable from a CLI, an MCP server, an HTTP
   API and a Python library. Nothing assumes a particular agent.
7. **Nothing sensitive in the repo.** Tokens, chat ids and paths live in the user's config
   and environment.

## Concepts

### Job

| Field | Meaning |
|---|---|
| `id` | Time-sortable id, e.g. `j_01J9Z3...`. |
| `queue` | Namespace (default `default`). Separate projects or teams share one Hopper by queue. |
| `title` | One line: what to do. |
| `body` | Full instructions and context. |
| `done_when` | Acceptance criteria: how the worker knows it is finished. |
| `priority` | 0–100, higher first. Named bands: `urgent` 100, `high` 70, `normal` 50, `low` 20, `background` 10, `idle` 5. |
| `requires` | Capabilities a worker must have to claim it, e.g. `["shell", "git"]`. |
| `tags` | Free-form labels for filtering. |
| `source` | Who submitted it, e.g. `telegram`, `cron`, `agent:<id>`. Informational. |
| `reply_to` | Where outcomes are delivered (see Sinks). |
| `key` | Idempotency key. Adding a job whose key matches an open job in the same queue returns the existing job instead of creating a duplicate. |
| `parent_id` | Set on child jobs created by splitting. |
| `depends_on` | Job ids that must finish (`done`) before this one can be claimed. |
| `not_before` | Earliest time it may run (scheduling, retry backoff). |
| `max_attempts` | Claims allowed before it is dead-lettered (default 3). |
| `timeout` | Seconds a worker may hold it without a heartbeat before the lease expires (default 900). |

### Lifecycle

```
            add
             │
             ▼
   ┌──────► queued ───claim──► running ──done──► done
   │         ▲  ▲                │  │  │
   │  answer │  │ lease expired  │  │  └─fail (retries left)──► queued (after backoff)
   │         │  │ / release      │  │
   │   needs_input ◄──ask────────┘  └──fail (no retries)──────► failed
   │                                │
   └── children all finished ◄── waiting ◄──split──┘
                       cancel (from any open state) ──► cancelled
```

- `queued`: waiting to be claimed. Claimable when `not_before` has passed, every
  `depends_on` job is `done`, and the worker has every capability in `requires`.
- `running`: claimed by one worker, with a lease that heartbeats extend.
- `needs_input`: the worker asked a question. The lease is released so the worker can do
  something else. `answer` puts the job back in the queue with the answer attached.
- `waiting`: the worker split it into child jobs. When every child is finished, the parent
  returns to `queued` and the next claimer sees the children's results.
- Terminal: `done`, `failed` (out of attempts, or failed with no retry), `cancelled`.

Open states are `queued`, `running`, `needs_input` and `waiting`; the active queue view
shows only those. Terminal jobs stay in history until `gc` trims them (default: 30 days).

### Claiming

A claim picks, in one transaction, the best job the worker can take:

1. Expired leases are reaped first: the job returns to `queued`, or to `failed` if it has
   used all its attempts.
2. Candidates: `queued`, in the worker's queues, `not_before` passed, all dependencies
   done, all `requires` satisfied by the worker's capabilities, and priority at least the
   worker's `min_priority` (used for reserved slots).
3. Order: effective priority descending, then oldest first. Effective priority is
   `priority + min(aging_cap, minutes_waiting / aging_minutes)` so starving work rises.
4. The job becomes `running` with `lease_owner = worker` and `lease_expires = now + timeout`,
   and `attempts` goes up by one.

### Results and evidence

`complete` takes a `summary` (what was done or found), an optional `evidence` (commands
run and their output, URLs checked, test results) and optional `artifacts` (paths, links).
A queue can be configured to require evidence.

### Splitting work across sub-agents

A worker that holds a big job can:

1. Add child jobs with `parent_id` set (and their own `requires`, so each goes to a
   capable worker). Children inherit the parent's queue, priority and reply routing unless
   given their own.
2. Call `split`/`wait`, which releases the parent into `waiting`.
3. When every child is finished, the parent is re-queued. Whoever claims it next reads the
   children's results (`show` includes them) and synthesizes.

Dependencies between children (`depends_on`) express ordering: "tests after the fix".

### Asking questions

`ask` records a question, parks the job in `needs_input`, and delivers the question to
`reply_to`. `answer` records the reply and re-queues the job. The next claimer sees the
full question/answer thread in the job.

## Sinks (reply_to)

`reply_to` is a URI. Hopper delivers `done`, `failed` and `needs_input` notices to it
through an outbox table, so deliveries survive crashes and are retried.

| Scheme | Delivery |
|---|---|
| `stdout:` | Printed by whichever Hopper process flushes the outbox. For testing. |
| `file:/abs/path.jsonl` | One JSON line appended per notice. |
| `webhook:https://...` | POST of the notice as JSON. |
| `telegram:<chat_id>` | Telegram message via the bot token in `HOPPER_TELEGRAM_TOKEN`. |
| `hook:<name>` | Runs a command named in the user's config (never a command from the job itself, because jobs can come from remote producers). The notice is passed on stdin as JSON. |

## Interfaces

All interfaces are thin layers over one `Client` API. A client is either local (opens the
SQLite file) or remote (talks to `hop serve` over HTTP with a token). The CLI and MCP
server work the same either way: set `HOPPER_URL` and `HOPPER_TOKEN` to go remote.

- **CLI `hop`**: `add`, `ls`, `show`, `claim`, `heartbeat`, `done`, `fail`, `ask`, `answer`,
  `split`, `release`, `cancel`, `bump`, `retry`, `stats`, `gc`, `serve`, `mcp`, `work`,
  `flush`, `init`. `--json` on every read.
- **MCP server `hop mcp`**: stdio JSON-RPC exposing the same operations as tools, so any
  MCP-capable harness (Claude Code, Hermes, OpenClaw, ...) can queue, claim and complete.
- **HTTP API `hop serve`**: JSON over HTTP, bearer-token auth with per-token scopes
  (`produce`, `work`, `admin`). Binds 127.0.0.1 unless told otherwise.
- **Python**: `from hopper import Hopper`.
- **Runner `hop work`**: turns any agent command into a worker. It claims jobs, renders
  the job into a prompt, runs the command (prompt on stdin and in a file), heartbeats while
  it runs, and completes or fails the job from the exit code if the agent didn't report
  itself. `--slots N --reserve K --reserve-min-priority 70` keeps K slots for urgent work.

## Configuration

`~/.config/hopper/config.toml` (or `$HOPPER_CONFIG`), all optional:

```toml
db = "~/.local/share/hopper/hopper.db"
history_days = 30

[queues.default]
require_evidence = false
aging_minutes = 30      # +1 effective priority per 30 minutes waiting
aging_cap = 20

[tokens]                 # for `hop serve`; name = { secret, scopes }
watch = { secret = "env:HOPPER_TOKEN_WATCH", scopes = ["produce"] }

[hooks]                  # for reply_to = "hook:<name>"
notify-me = ["/usr/local/bin/notify-me"]
```

Secrets are referenced as `env:NAME` so the file itself holds none.

## Scaling

- One host: many workers share the SQLite file (WAL mode, short transactions, `BEGIN
  IMMEDIATE` claims). Comfortable into the thousands of jobs per hour.
- Many hosts: run `hop serve` next to the database; everything else points at it.
- The store is one module behind a small interface, so a Postgres backend
  (`FOR UPDATE SKIP LOCKED`) can be added without touching the interfaces.

## Out of scope for v0

Postgres backend, a web UI, cron-style recurring jobs (use the system scheduler to `hop
add`), multi-tenant auth beyond scoped tokens.
