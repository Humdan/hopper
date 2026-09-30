"""`hop`: the Hopper command line."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import time

from . import __version__
from .client import connect
from .prompt import render
from .util import HopperError, ago, iso, priority_name

STATUS_MARK = {
    "queued": "·", "running": "▶", "needs_input": "?", "waiting": "…",
    "done": "✓", "failed": "✗", "cancelled": "–",
}

SAMPLE_CONFIG = """\
# Hopper config. Every setting is optional; secrets are referenced as "env:NAME".
# db = "~/.local/share/hopper/hopper.db"
# history_days = 30              # finished jobs are trimmed after this (hop gc)

# [queues.default]
# require_evidence = false       # refuse `hop done` without --evidence
# aging_minutes = 30             # waiting jobs gain +1 priority per 30 minutes...
# aging_cap = 20                 # ...up to +20, so low-priority work is never starved

# Tokens for `hop serve`. Scopes: read, produce, work, admin.
# [tokens]
# phone = { secret = "env:HOPPER_TOKEN_PHONE", scopes = ["produce"] }
# laptop-worker = { secret = "env:HOPPER_TOKEN_LAPTOP", scopes = ["work"] }

# Where job outcomes may be delivered (reply_to). Anything not listed is refused.
# [sinks.telegram]
# chats = ["123456789"]           # bot token comes from $HOPPER_TELEGRAM_TOKEN
# [sinks.webhook]
# allow = ["https://example.com/hooks/"]
# [hooks]                         # reply_to = "hook:notify" runs this command
# notify = ["/usr/local/bin/notify-me"]
"""


def _default_worker() -> str:
    return os.environ.get("HOPPER_WORKER") or f"{getpass.getuser()}@{socket.gethostname()}"


def _csv(value):
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _read_text(value: str | None) -> str | None:
    """'-' reads stdin, '@file' reads a file, anything else is taken literally."""
    if value == "-":
        return sys.stdin.read()
    if value and value.startswith("@") and os.path.exists(value[1:]):
        with open(value[1:]) as fh:
            return fh.read()
    return value


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _row(job: dict) -> str:
    mark = STATUS_MARK.get(job["status"], "?")
    scheduled = job["status"] == "queued" and (job.get("not_before") or 0) > time.time()
    when = ago(job["not_before"]) if scheduled else ago(job["created_at"])
    owner = f" @{job['lease_owner']}" if job["status"] == "running" else ""
    return (f"{mark} {job['id']}  p{job['priority']:<3} {job['status']:<11} {when:>6}  "
            f"{job['queue']:<10} {job['title'][:70]}{owner}")


def _print_job(job: dict, events=None) -> None:
    print(f"{job['id']}  [{job['status']}]  {job['title']}")
    print(f"  queue {job['queue']} · priority {job['priority']} ({priority_name(job['priority'])})"
          f" · attempts {job['attempts']}/{job['max_attempts']} · created {ago(job['created_at'])} ago")
    for label, key in (("source", "source"), ("reply to", "reply_to"), ("key", "key"),
                       ("parent", "parent_id"), ("held by", "lease_owner")):
        if job.get(key):
            print(f"  {label}: {job[key]}")
    if job.get("requires"):
        print(f"  requires: {', '.join(job['requires'])}")
    if job.get("tags"):
        print(f"  tags: {', '.join(job['tags'])}")
    if job.get("depends_on"):
        print(f"  after: {', '.join(job['depends_on'])}")
    if job.get("not_before"):
        print(f"  not before: {iso(job['not_before'])}")
    if job.get("body"):
        print("\n" + job["body"].rstrip())
    if job.get("done_when"):
        print(f"\nDone when: {job['done_when']}")
    for t in job.get("thread") or []:
        print(f"\n{'Q' if t['kind'] == 'question' else 'A'} ({ago(t['at'])} ago): {t['text']}")
    if job.get("children"):
        print("\nSub-jobs:")
        for c in job["children"]:
            res = (c.get("result") or {}).get("summary") or ""
            print(f"  {STATUS_MARK.get(c['status'], '?')} {c['id']} {c['title']}"
                  + (f": {res[:100]}" if res else ""))
    if job.get("result"):
        r = job["result"]
        print(f"\nResult: {r.get('summary', '')}")
        if r.get("evidence"):
            print(f"Evidence: {r['evidence']}")
        for a in r.get("artifacts") or []:
            print(f"  - {a}")
    if events:
        print("\nHistory:")
        for e in events:
            extra = {k: v for k, v in e.items() if k not in ("at", "kind", "actor")}
            print(f"  {iso(e['at'])} {e['kind']:<16} {e['actor'] or ''}"
                  + (f" {json.dumps(extra)}" if extra else ""))


def _parse_child(spec: str) -> dict:
    title, sep, body = spec.partition("::")
    return {"title": title.strip(), "body": body.strip()} if sep else {"title": spec.strip()}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hop", description="Hopper: a durable priority job queue for AI agents.")
    p.add_argument("--version", action="version", version=f"hopper {__version__}")
    p.add_argument("--db", help="database file (default: config, $HOPPER_DB, or ~/.local/share/hopper)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def cmd(name, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, **kw)
        return sp

    def worker_arg(sp):
        sp.add_argument("--worker", default=None, help="your worker name (default: $HOPPER_WORKER or user@host)")

    sp = cmd("init", "create the database and a sample config")

    sp = cmd("add", "queue a job")
    sp.add_argument("title")
    sp.add_argument("-b", "--body", help="instructions ('-' for stdin, @file to read a file)")
    sp.add_argument("-d", "--done-when", help="how the worker knows it's finished")
    sp.add_argument("-p", "--priority", help="urgent|high|normal|low|background|idle or 0-100")
    sp.add_argument("-q", "--queue", default="default")
    sp.add_argument("-r", "--requires", help="capabilities needed, comma-separated (e.g. shell,git)")
    sp.add_argument("-t", "--tags", help="comma-separated tags")
    sp.add_argument("--reply-to", help="where to deliver the outcome, e.g. telegram:<chat>")
    sp.add_argument("--key", help="idempotency key: re-adding while open returns the same job")
    sp.add_argument("--after", help="job ids that must finish first, comma-separated")
    sp.add_argument("--at", dest="not_before", help="earliest start: ISO time, epoch, or +10m")
    sp.add_argument("--timeout", default="15m", help="lease length without a heartbeat (default 15m)")
    sp.add_argument("--attempts", type=int, default=3, help="claims before it's failed (default 3)")
    sp.add_argument("--source", help="who is asking (default: $HOPPER_SOURCE or user@host)")
    sp.add_argument("--json", action="store_true")

    sp = cmd("ls", "list jobs (open ones by default)")
    sp.add_argument("-s", "--status", default="open", help="open, all, or a status (comma-separated)")
    sp.add_argument("-q", "--queue")
    sp.add_argument("-t", "--tags")
    sp.add_argument("-n", "--limit", type=int, default=50)
    sp.add_argument("--json", action="store_true")

    sp = cmd("show", "show one job")
    sp.add_argument("job_id")
    sp.add_argument("--history", action="store_true", help="include the event history")
    sp.add_argument("--brief", action="store_true", help="print the brief a worker would get")
    sp.add_argument("--json", action="store_true")

    sp = cmd("claim", "take the most important job you can do")
    worker_arg(sp)
    sp.add_argument("-c", "--caps", help="your capabilities, comma-separated")
    sp.add_argument("-q", "--queues", help="queues to take from (default: default)")
    sp.add_argument("-t", "--tags", help="only jobs with one of these tags")
    sp.add_argument("--min-priority", default=0)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--brief", action="store_true", help="print the brief instead of the summary")

    sp = cmd("heartbeat", "keep your claim alive")
    sp.add_argument("job_id")
    worker_arg(sp)
    sp.add_argument("--progress", help="a note on where you are")
    sp.add_argument("--extend", help="lease length from now (default: the job's timeout)")

    sp = cmd("done", "complete your job")
    sp.add_argument("job_id")
    worker_arg(sp)
    sp.add_argument("-m", "--summary", required=True, help="what you did or found ('-' for stdin)")
    sp.add_argument("-e", "--evidence", help="what you ran or checked, and what you saw")
    sp.add_argument("-a", "--artifact", action="append", help="a path or link produced (repeatable)")

    sp = cmd("fail", "give up on your job")
    sp.add_argument("job_id")
    worker_arg(sp)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--no-retry", action="store_true", help="fail for good instead of re-queuing")

    sp = cmd("release", "hand your job back untouched")
    sp.add_argument("job_id")
    worker_arg(sp)
    sp.add_argument("--reason")

    sp = cmd("ask", "ask the requester a question; the job waits for the answer")
    sp.add_argument("job_id")
    sp.add_argument("question")
    worker_arg(sp)

    sp = cmd("answer", "answer a job's question; it goes back to the queue")
    sp.add_argument("job_id")
    sp.add_argument("text")

    sp = cmd("split", "split your job into sub-jobs; it resumes when they finish")
    sp.add_argument("job_id")
    worker_arg(sp)
    sp.add_argument("--child", action="append", default=[], help='"title :: instructions" (repeatable)')
    sp.add_argument("--child-json", help="a JSON list of child jobs ('-' for stdin, @file)")

    sp = cmd("wait", "park your job until its separately-added children finish")
    sp.add_argument("job_id")
    worker_arg(sp)

    sp = cmd("cancel", "cancel a job and its open sub-jobs")
    sp.add_argument("job_id")
    sp.add_argument("--reason")

    sp = cmd("bump", "change a job's priority")
    sp.add_argument("job_id")
    sp.add_argument("priority")

    sp = cmd("retry", "give a failed or cancelled job a fresh start")
    sp.add_argument("job_id")

    sp = cmd("stats", "queue counts and health")
    sp.add_argument("--json", action="store_true")

    sp = cmd("gc", "trim finished jobs older than N days")
    sp.add_argument("--days", type=float)

    cmd("flush", "deliver pending notices now")

    sp = cmd("serve", "serve the queue over HTTP")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8770)
    sp.add_argument("--no-auth", action="store_true", help="skip tokens (localhost only)")
    sp.add_argument("--quiet", action="store_true")

    cmd("mcp", "run the MCP server on stdio (for agent harnesses)")

    sp = cmd("work", "run an agent command as a worker")
    sp.add_argument("--exec", dest="command", required=True,
                    help="command to run per job; placeholders {prompt_file} {job_id} {title}; "
                         "the brief is also on stdin")
    sp.add_argument("--shell", action="store_true", help="run --exec through /bin/sh")
    sp.add_argument("--name", help="worker name (default host-pid)")
    sp.add_argument("-c", "--caps", help="capabilities this worker has, comma-separated")
    sp.add_argument("-q", "--queues", help="queues to take from")
    sp.add_argument("-t", "--tags", help="only jobs with one of these tags")
    sp.add_argument("--slots", type=int, default=1, help="jobs at once (default 1)")
    sp.add_argument("--reserve", type=int, default=0, help="slots kept for urgent work")
    sp.add_argument("--reserve-min-priority", default="high", help="what counts as urgent (default high=70)")
    sp.add_argument("--poll", type=float, default=5.0, help="seconds between claims when idle")
    sp.add_argument("--max-runtime", default="1h", help="stop an agent after this long (default 1h)")
    sp.add_argument("--once", action="store_true", help="do at most one job, then exit")
    sp.add_argument("--log-dir")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except HopperError as exc:
        print(f"hop: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


def _run(a) -> int:
    if a.cmd == "mcp":
        from .mcp import run
        run()
        return 0

    hop = connect(a.db)
    worker = getattr(a, "worker", None) or _default_worker()

    if a.cmd == "init":
        from .config import _default_config_path
        path = os.environ.get("HOPPER_CONFIG") or _default_config_path()
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(SAMPLE_CONFIG)
            print(f"wrote sample config {path}")
        print(f"database ready: {hop.db_path}")
        return 0

    if a.cmd == "add":
        job = hop.add(title=a.title, body=_read_text(a.body) or "", done_when=a.done_when,
                      priority=a.priority, queue=a.queue, requires=_csv(a.requires),
                      tags=_csv(a.tags), reply_to=a.reply_to, key=a.key,
                      depends_on=_csv(a.after), not_before=a.not_before, timeout=a.timeout,
                      max_attempts=a.attempts,
                      source=a.source or os.environ.get("HOPPER_SOURCE") or _default_worker())
        if a.json:
            _dump(job)
        else:
            note = " (already queued; key matched)" if job.get("deduplicated") else ""
            print(f"{job['id']}{note}")
        return 0

    if a.cmd == "ls":
        jobs = hop.list(status=a.status, queue=a.queue, tags=_csv(a.tags), limit=a.limit)
        if a.json:
            _dump(jobs)
        elif not jobs:
            print("nothing here" if a.status != "open" else "queue is empty")
        else:
            for j in jobs:
                print(_row(j))
        return 0

    if a.cmd == "show":
        job = hop.show(job_id=a.job_id)
        events = hop.events(job_id=a.job_id) if (a.history or a.brief or a.json) else None
        if a.json:
            job["history"] = events
            _dump(job)
        elif a.brief:
            print(render(job, events=events))
        else:
            _print_job(job, events if a.history else None)
        return 0

    if a.cmd == "claim":
        job = hop.claim(worker=worker, caps=_csv(a.caps), queues=_csv(a.queues),
                        tags=_csv(a.tags), min_priority=a.min_priority)
        if not job:
            if a.json:
                print("null")
            else:
                print("nothing to claim", file=sys.stderr)
            return 2
        if a.json:
            job["brief"] = render(job, events=hop.events(job_id=job["id"]))
            _dump(job)
        elif a.brief:
            print(render(job, events=hop.events(job_id=job["id"])))
        else:
            print(_row(job))
        return 0

    simple = {
        "heartbeat": lambda: hop.heartbeat(job_id=a.job_id, worker=worker, extend=a.extend,
                                           progress=a.progress),
        "done": lambda: hop.complete(job_id=a.job_id, worker=worker, summary=_read_text(a.summary),
                                     evidence=_read_text(a.evidence), artifacts=a.artifact),
        "fail": lambda: hop.fail(job_id=a.job_id, worker=worker, reason=a.reason,
                                 retry=not a.no_retry),
        "release": lambda: hop.release(job_id=a.job_id, worker=worker, reason=a.reason),
        "ask": lambda: hop.ask(job_id=a.job_id, worker=worker, question=a.question),
        "answer": lambda: hop.answer(job_id=a.job_id, text=a.text, actor=_default_worker()),
        "wait": lambda: hop.wait(job_id=a.job_id, worker=worker),
        "cancel": lambda: hop.cancel(job_id=a.job_id, reason=a.reason, actor=_default_worker()),
        "bump": lambda: hop.bump(job_id=a.job_id, priority=a.priority, actor=_default_worker()),
        "retry": lambda: hop.retry(job_id=a.job_id, actor=_default_worker()),
    }
    if a.cmd in simple:
        print(_row(simple[a.cmd]()))
        return 0

    if a.cmd == "split":
        children = [_parse_child(c) for c in a.child]
        if a.child_json:
            extra = json.loads(_read_text(a.child_json))
            if not isinstance(extra, list):
                raise HopperError("--child-json must be a JSON list")
            children += extra
        job = hop.split(job_id=a.job_id, worker=worker, children=children)
        print(_row(job))
        for c in job.get("children") or []:
            print(f"  {STATUS_MARK.get(c['status'], '?')} {c['id']} {c['title']}")
        return 0

    if a.cmd == "stats":
        s = hop.stats()
        if a.json:
            _dump(s)
            return 0
        if not s["queues"]:
            print("no jobs yet")
        for q, counts in sorted(s["queues"].items()):
            parts = [f"{n} {st}" for st, n in sorted(counts.items())]
            print(f"{q}: {', '.join(parts)}")
        if s.get("oldest_queued_s") is not None:
            print(f"oldest waiting job: {s['oldest_queued_s'] // 60} min")
        if s.get("outbox_pending") or s.get("outbox_stuck"):
            print(f"notices: {s['outbox_pending']} pending, {s['outbox_stuck']} undeliverable")
        return 0

    if a.cmd == "gc":
        print(f"removed {hop.gc(days=a.days)['removed']} finished job(s)")
        return 0

    if a.cmd == "flush":
        r = hop.flush()
        print(f"sent {r['sent']}, failed {r['failed']}")
        return 0

    if a.cmd == "serve":
        if os.environ.get("HOPPER_URL"):
            raise HopperError("hop serve needs a local database; unset HOPPER_URL")
        from .server import serve
        serve(hop, host=a.host, port=a.port, no_auth=a.no_auth, quiet=a.quiet)
        return 0

    if a.cmd == "work":
        from .util import parse_duration
        from .worker import Runner
        runner = Runner(hop, a.command, name=a.name, shell=a.shell, caps=_csv(a.caps),
                        queues=_csv(a.queues), tags=_csv(a.tags), slots=a.slots,
                        reserve=a.reserve, reserve_min_priority=a.reserve_min_priority,
                        poll=a.poll, max_runtime=parse_duration(a.max_runtime), once=a.once,
                        log_dir=a.log_dir)
        return runner.run()

    raise HopperError(f"unknown command {a.cmd}")


if __name__ == "__main__":
    sys.exit(main())
