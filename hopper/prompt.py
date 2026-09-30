"""Turning a job into the brief an agent actually works from.

This is written the way an agent wants work handed over: the goal first, what "done"
means, everything already learned (answers, sub-job results, earlier attempts), and the
exact commands for reporting back. Harness-neutral: it names both the CLI and the MCP
tools so it works whichever the agent has.
"""
from __future__ import annotations

from .util import ago, priority_name


def render(job: dict, worker: str | None = None, events: list[dict] | None = None) -> str:
    jid = job["id"]
    lines = [
        f"You have been assigned job {jid} from Hopper, a shared job queue.",
        f"Queue: {job['queue']} · priority {job['priority']} ({priority_name(job['priority'])})"
        f" · attempt {job['attempts']} of {job['max_attempts']}"
        + (f" · requested by {job['source']}" if job.get("source") else ""),
        "",
        f"# {job['title']}",
        "",
        (job.get("body") or "").strip() or "(No further details were given.)",
        "",
        "## Done when",
        (job.get("done_when") or "").strip()
        or "Not specified. Decide what a complete, verified result looks like, and say so in your summary.",
    ]

    thread = job.get("thread") or []
    if thread:
        lines += ["", "## Conversation so far"]
        for t in thread:
            who = "You asked" if t["kind"] == "question" else "Answer"
            lines.append(f"- {who} ({ago(t['at'])} ago): {t['text']}")

    children = job.get("children") or []
    if children:
        lines += ["", "## Sub-jobs you split this into (all finished)"]
        for c in children:
            res = (c.get("result") or {}).get("summary") or "(no result recorded)"
            lines.append(f"- [{c['status']}] {c['title']} ({c['id']}): {res}")
        lines.append("Use these results; don't redo the sub-jobs.")

    earlier = [e for e in (events or []) if e["kind"] in ("retry_scheduled", "lease_expired")]
    if earlier:
        lines += ["", "## Earlier attempts"]
        for e in earlier:
            why = e.get("reason") or ("the worker stopped responding" if e["kind"] == "lease_expired" else "")
            lines.append(f"- {e['kind'].replace('_', ' ')}: {why}")
        lines.append("Avoid repeating whatever went wrong.")

    w = f" --worker {worker}" if worker else ""
    lines += [
        "",
        "## Reporting back (required)",
        "Finish by doing exactly one of these. Use the `hop` command, or the matching",
        "hopper MCP tool (hop_complete, hop_ask, hop_split, hop_fail) if you have those.",
        "",
        f"- Done: `hop done {jid}{w} --summary \"what you did or found\" --evidence \"what you ran or checked, and what you saw\"`",
        "  Evidence means something checked now (a command and its output, a test run, a URL you",
        "  loaded), not an assumption and not an old log.",
        f"- Need an answer from the requester: `hop ask {jid}{w} \"your question\"`, then stop.",
        "  The job waits for the answer and comes back to a worker with it.",
        f"- Too big for one pass: `hop split {jid}{w} --child \"title :: instructions\" ...`, then stop.",
        "  Add `--requires shell,git` style capabilities per child with --child-json for more",
        "  control. You (or another worker) get this job back with every sub-job's result.",
        f"- Can't be done: `hop fail {jid}{w} --reason \"why\" --no-retry`",
        f"  (drop --no-retry if trying again later could work).",
        f"- Long task: `hop heartbeat {jid}{w} --progress \"where you are\"` every few minutes keeps",
        f"  your claim (it expires after {int(job['timeout'])}s without one).",
        "",
        "If you exit without reporting, the runner records your final output as the result",
        "(exit code 0) or retries the job (non-zero).",
    ]
    return "\n".join(lines)
