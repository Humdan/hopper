---
name: hopper
description: Use Hopper, the shared job queue, to hand work off, take work on, split big jobs across sub-agents, and report results with evidence. Use when asked to queue, delegate, or pick up work, or when a job id like j_01... appears.
---

# Hopper: working from a shared job queue

Hopper holds jobs that people and agents hand off. Use the `hop` command (or the
`hop_*` MCP tools, which take the same arguments).

## Handing work off

```sh
hop add "Short title of the job" \
  -b "Full instructions: what, where, context, constraints" \
  -d "How to tell it's done (what to check)" \
  -p normal            # urgent | high | normal | low | background | idle, or 0-100
  # optional: -r shell,git (capabilities needed)  --key <dedupe-key>  --after <job-id>
  #           --at +2h (start later)  --reply-to telegram:<chat>|webhook:...|file:...
  #           -P <project> (defaults to the git repo you're in; used by `hop report`)
```

Write the job so a fresh agent with no context can finish it: say what, where, and how to
verify. Give recurring or retryable work a `--key` so re-adding it never creates a duplicate.

## Taking work on

```sh
hop claim --caps shell,git,web --brief   # prints the brief, or exits 2 if nothing is waiting
```

Then finish with exactly one of:

| Situation | Command |
|---|---|
| Finished | `hop done <id> -m "what you did or found" -e "what you ran/checked just now, and what you saw"` |
| Need the requester to decide | `hop ask <id> "question"`, then stop. The job returns with the answer. |
| Too big for one pass | `hop split <id> --child "title :: instructions" --child "..."`, then stop. It returns with each child's result. |
| Missing a capability | `hop release <id> --reason "needs X"` (no attempt is used up) |
| Can't be done | `hop fail <id> --reason "why" --no-retry` |
| Long task | `hop heartbeat <id> --progress "where you are"` every few minutes |

Rules that keep the queue trustworthy:

- **Evidence is something you checked now**: a command and its output, a test run, a page you
  loaded. Not an assumption, not an old log, not a guess about a URL.
- **One job, one owner.** Only act on jobs you claimed. `hop show <id>` to read any job.
- **Don't redo sub-jobs.** When a split job comes back, its brief lists every child's result;
  build on them.

## Splitting well

Use `--child-json` for control over each child:

```sh
hop split <id> --child-json '[
  {"title": "Research options", "requires": ["web"], "done_when": "3 options with sources"},
  {"title": "Implement the chosen one", "requires": ["shell","git"], "depends_on": ["#0"]},
  {"title": "Test it", "requires": ["shell"], "depends_on": ["#1"]}
]'
```

`#0`, `#1` refer to earlier children, so work runs in order where it must and in parallel
where it can. Children inherit the parent's queue and priority.

## Looking around

```sh
hop ls                 # open jobs: running first, then what's next
hop show <id> --history
hop stats
hop report --since 7d  # time and cost by project (--by day|week|queue|worker)
```
