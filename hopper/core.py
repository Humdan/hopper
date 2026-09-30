"""The queue itself: a SQLite-backed store with atomic, leased claims.

Every public method is one short transaction. Writes use BEGIN IMMEDIATE so concurrent
workers serialize on the write lock instead of racing, and WAL mode lets readers
continue meanwhile. See docs/design.md for the model.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from . import sinks
from .config import Config, load as load_config
from .util import (ALL_STATES, OPEN_STATES, TERMINAL_STATES, HopperError, new_id, now,
                   parse_duration, parse_priority, parse_when)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  queue         TEXT NOT NULL DEFAULT 'default',
  title         TEXT NOT NULL,
  body          TEXT NOT NULL DEFAULT '',
  done_when     TEXT,
  priority      INTEGER NOT NULL DEFAULT 50,
  status        TEXT NOT NULL DEFAULT 'queued',
  requires      TEXT NOT NULL DEFAULT '[]',
  tags          TEXT NOT NULL DEFAULT '[]',
  source        TEXT,
  reply_to      TEXT,
  key           TEXT,
  parent_id     TEXT,
  not_before    REAL,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  attempts      INTEGER NOT NULL DEFAULT 0,
  timeout       REAL NOT NULL DEFAULT 900,
  lease_owner   TEXT,
  lease_expires REAL,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  started_at    REAL,
  finished_at   REAL,
  result        TEXT,
  thread        TEXT NOT NULL DEFAULT '[]',
  meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(status, queue, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_id);
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs(lease_expires) WHERE status = 'running';
CREATE UNIQUE INDEX IF NOT EXISTS jobs_key_open ON jobs(queue, key)
  WHERE key IS NOT NULL AND status IN ('queued','running','needs_input','waiting');

CREATE TABLE IF NOT EXISTS deps (
  job_id     TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (job_id, depends_on)
);
CREATE INDEX IF NOT EXISTS deps_rev ON deps(depends_on);

CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  at     REAL NOT NULL,
  kind   TEXT NOT NULL,
  actor  TEXT,
  data   TEXT
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);

CREATE TABLE IF NOT EXISTS outbox (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id       TEXT NOT NULL,
  target       TEXT NOT NULL,
  payload      TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_at      REAL NOT NULL,
  delivered_at REAL,
  last_error   TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(next_at) WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

_JSON_FIELDS = ("requires", "tags", "result", "thread", "meta")
_OUTBOX_GIVE_UP = 10  # delivery attempts before a notice is left for inspection


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for f in _JSON_FIELDS:
        if f in d and d[f] is not None:
            d[f] = json.loads(d[f])
    return d


def _list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in (s.strip() for s in value.split(",")) if v]
    return sorted({str(v) for v in value})


class Hopper:
    """A local Hopper: opens (and creates) the SQLite database directly."""

    def __init__(self, db: str | None = None, config: Config | None = None):
        self.cfg = config or load_config()
        self.db_path = db or self.cfg.db
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema', ?)",
                         (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self._db.close()

    # ── plumbing ───────────────────────────────────────────────────────────────

    @contextmanager
    def _tx(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def _event(self, db, job_id: str, kind: str, actor: str | None = None, **data) -> None:
        db.execute("INSERT INTO events(job_id, at, kind, actor, data) VALUES (?,?,?,?,?)",
                   (job_id, now(), kind, actor, json.dumps(data) if data else None))

    def _get(self, db, job_id: str) -> dict:
        job = _row(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        if job is None:
            raise HopperError(f"no job {job_id}")
        return job

    def _owned(self, db, job_id: str, worker: str) -> dict:
        job = self._get(db, job_id)
        if job["status"] != "running":
            raise HopperError(f"{job_id} is {job['status']}, not running")
        if job["lease_owner"] != worker:
            raise HopperError(f"{job_id} is held by {job['lease_owner']}, not {worker}")
        return job

    def _set(self, db, job_id: str, **fields) -> None:
        fields["updated_at"] = now()
        for f in _JSON_FIELDS:
            if f in fields and fields[f] is not None and not isinstance(fields[f], str):
                fields[f] = json.dumps(fields[f])
        cols = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def _notify(self, db, job: dict, event: str, text: str | None = None) -> None:
        if not job.get("reply_to"):
            return
        notice = {
            "event": event,
            "at": now(),
            "text": text,
            "job": {k: job.get(k) for k in ("id", "queue", "title", "priority", "source", "status")},
        }
        db.execute("INSERT INTO outbox(job_id, target, payload, next_at) VALUES (?,?,?,?)",
                   (job["id"], job["reply_to"], json.dumps(notice), now()))

    def _finish(self, db, job: dict, status: str, actor: str, *, result=None,
                reason: str | None = None) -> None:
        """Move a job to a terminal state and ripple the consequences."""
        self._set(db, job["id"], status=status, finished_at=now(), lease_owner=None,
                  lease_expires=None, **({"result": result} if result is not None else {}))
        self._event(db, job["id"], status, actor, **({"reason": reason} if reason else {}))
        job = self._get(db, job["id"])
        text = (result or {}).get("summary") if status == "done" else reason
        self._notify(db, job, status, text)

        if status in ("failed", "cancelled"):
            # Dependents can never run now; fail them with a clear reason.
            for (dep_id,) in db.execute(
                    "SELECT d.job_id FROM deps d JOIN jobs j ON j.id = d.job_id "
                    "WHERE d.depends_on = ? AND j.status IN ('queued','needs_input')",
                    (job["id"],)).fetchall():
                self._finish(db, self._get(db, dep_id), "failed", "hopper",
                             reason=f"dependency {job['id']} {status}")
            if status == "cancelled":
                for (child_id,) in db.execute(
                        "SELECT id FROM jobs WHERE parent_id = ? AND status IN "
                        "('queued','running','needs_input','waiting')", (job["id"],)).fetchall():
                    self._finish(db, self._get(db, child_id), "cancelled", actor,
                                 reason=f"parent {job['id']} cancelled")

        if job.get("parent_id"):
            self._maybe_resume_parent(db, job["parent_id"])

    def _maybe_resume_parent(self, db, parent_id: str) -> None:
        parent = _row(db.execute("SELECT * FROM jobs WHERE id = ?", (parent_id,)).fetchone())
        if not parent or parent["status"] != "waiting":
            return
        (open_children,) = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE parent_id = ? AND status IN "
            "('queued','running','needs_input','waiting')", (parent_id,)).fetchone()
        if open_children == 0:
            self._set(db, parent_id, status="queued")
            self._event(db, parent_id, "resumed", "hopper", reason="all children finished")

    def _reap(self, db) -> int:
        """Expired leases: back to the queue, or dead if out of attempts."""
        expired = db.execute(
            "SELECT * FROM jobs WHERE status = 'running' AND lease_expires < ?", (now(),)).fetchall()
        for r in expired:
            job = _row(r)
            if job["attempts"] >= job["max_attempts"]:
                self._finish(db, job, "failed", "hopper",
                             reason=f"lease expired on attempt {job['attempts']}/{job['max_attempts']}"
                                    f" (last worker {job['lease_owner']})")
            else:
                self._set(db, job["id"], status="queued", lease_owner=None, lease_expires=None)
                self._event(db, job["id"], "lease_expired", "hopper", worker=job["lease_owner"])
        return len(expired)

    # ── producing ──────────────────────────────────────────────────────────────

    def add(self, title: str, body: str = "", *, queue: str = "default", priority=None,
            requires=None, tags=None, source: str | None = None, reply_to: str | None = None,
            key: str | None = None, parent_id: str | None = None, depends_on=None,
            not_before=None, max_attempts: int = 3, timeout="15m",
            done_when: str | None = None, meta: dict | None = None,
            actor: str | None = None) -> dict:
        """Queue a job. With a key that matches an open job, returns that job instead."""
        with self._tx() as db:
            return self._insert(db, title, body, queue=queue, priority=priority,
                                requires=requires, tags=tags, source=source, reply_to=reply_to,
                                key=key, parent_id=parent_id, depends_on=depends_on,
                                not_before=not_before, max_attempts=max_attempts,
                                timeout=timeout, done_when=done_when, meta=meta, actor=actor)

    def _insert(self, db, title: str, body: str = "", *, queue: str = "default", priority=None,
                requires=None, tags=None, source=None, reply_to=None, key=None,
                parent_id=None, depends_on=None, not_before=None, max_attempts: int = 3,
                timeout="15m", done_when=None, meta=None, actor=None) -> dict:
        title = (title or "").strip()
        if not title:
            raise HopperError("a job needs a title")
        if int(max_attempts) < 1:
            raise HopperError("max_attempts must be at least 1")
        timeout_s = parse_duration(timeout)
        if timeout_s < 5:
            raise HopperError("timeout must be at least 5 seconds")
        reply_to = sinks.validate(reply_to, self.cfg)
        deps = _list(depends_on)
        ts = now()
        if parent_id:
            parent = self._get(db, parent_id)
            queue = queue if queue != "default" else parent["queue"]
            if priority is None:
                priority = parent["priority"]
        prio = parse_priority(priority)
        if key:
            existing = db.execute(
                "SELECT id FROM jobs WHERE queue = ? AND key = ? AND status IN "
                "('queued','running','needs_input','waiting')", (queue, key)).fetchone()
            if existing:
                job = self._get(db, existing[0])
                job["deduplicated"] = True
                return job
        for d in deps:
            self._get(db, d)  # every dependency must exist
        job_id = new_id()
        db.execute(
            "INSERT INTO jobs(id, queue, title, body, done_when, priority, requires, tags,"
            " source, reply_to, key, parent_id, not_before, max_attempts, timeout,"
            " created_at, updated_at, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, queue, title, body or "", done_when, prio, json.dumps(_list(requires)),
             json.dumps(_list(tags)), source, reply_to, key, parent_id,
             parse_when(not_before), int(max_attempts), timeout_s, ts, ts, json.dumps(meta or {})))
        for d in deps:
            db.execute("INSERT INTO deps(job_id, depends_on) VALUES (?,?)", (job_id, d))
        self._event(db, job_id, "added", actor or source, priority=prio)
        return self._get(db, job_id)

    def cancel(self, job_id: str, reason: str | None = None, actor: str | None = None) -> dict:
        with self._tx() as db:
            job = self._get(db, job_id)
            if job["status"] in TERMINAL_STATES:
                raise HopperError(f"{job_id} is already {job['status']}")
            self._finish(db, job, "cancelled", actor or "user", reason=reason or "cancelled")
            return self._get(db, job_id)

    def bump(self, job_id: str, priority, actor: str | None = None) -> dict:
        prio = parse_priority(priority)
        with self._tx() as db:
            job = self._get(db, job_id)
            if job["status"] in TERMINAL_STATES:
                raise HopperError(f"{job_id} is {job['status']}")
            self._set(db, job_id, priority=prio)
            self._event(db, job_id, "bumped", actor, frm=job["priority"], to=prio)
            return self._get(db, job_id)

    def answer(self, job_id: str, text: str, actor: str | None = None) -> dict:
        with self._tx() as db:
            job = self._get(db, job_id)
            if job["status"] != "needs_input":
                raise HopperError(f"{job_id} is {job['status']}; nothing is waiting for an answer")
            thread = job["thread"] + [{"at": now(), "kind": "answer", "by": actor, "text": text}]
            self._set(db, job_id, status="queued", thread=thread)
            self._event(db, job_id, "answered", actor)
            return self._get(db, job_id)

    def retry(self, job_id: str, actor: str | None = None) -> dict:
        """Give a failed or cancelled job a fresh set of attempts."""
        with self._tx() as db:
            job = self._get(db, job_id)
            if job["status"] not in ("failed", "cancelled"):
                raise HopperError(f"{job_id} is {job['status']}; only failed or cancelled jobs can be retried")
            self._set(db, job_id, status="queued", attempts=0, finished_at=None,
                      not_before=None, result=None)
            self._event(db, job_id, "retried", actor)
            return self._get(db, job_id)

    # ── working ────────────────────────────────────────────────────────────────

    def claim(self, worker: str, caps=None, queues=None, min_priority=0,
              tags=None) -> dict | None:
        """Atomically take the best job this worker can do, or None."""
        if not worker:
            raise HopperError("claim needs a worker name")
        caps = _list(caps)
        queues = _list(queues) or ["default"]
        min_p = parse_priority(min_priority) if min_priority else 0
        t = now()
        with self._tx() as db:
            self._reap(db)
            q_marks = ",".join("?" * len(queues))
            sql = f"""
              SELECT j.* FROM jobs j
              WHERE j.status = 'queued'
                AND j.queue IN ({q_marks})
                AND (j.not_before IS NULL OR j.not_before <= ?)
                AND j.priority >= ?
                AND NOT EXISTS (SELECT 1 FROM json_each(j.requires) r
                                WHERE r.value NOT IN (SELECT value FROM json_each(?)))
                AND NOT EXISTS (SELECT 1 FROM deps d JOIN jobs p ON p.id = d.depends_on
                                WHERE d.job_id = j.id AND p.status != 'done')
            """
            params: list = [*queues, t, min_p, json.dumps(caps)]
            if tags:
                sql += (" AND EXISTS (SELECT 1 FROM json_each(j.tags) g "
                        "WHERE g.value IN (SELECT value FROM json_each(?)))")
                params.append(json.dumps(_list(tags)))
            candidates = [_row(r) for r in db.execute(sql, params).fetchall()]
            if not candidates:
                return None

            def effective(job):
                pol = self.cfg.policy(job["queue"])
                waited_min = (t - job["created_at"]) / 60
                bonus = min(pol.aging_cap, waited_min / pol.aging_minutes) if pol.aging_minutes > 0 else 0
                return (-(job["priority"] + bonus), job["created_at"])

            job = min(candidates, key=effective)
            self._set(db, job["id"], status="running", lease_owner=worker,
                      lease_expires=t + job["timeout"], attempts=job["attempts"] + 1,
                      started_at=job["started_at"] or t)
            self._event(db, job["id"], "claimed", worker, attempt=job["attempts"] + 1)
            return self.show(job["id"], _db=db)

    def heartbeat(self, job_id: str, worker: str, extend=None, progress: str | None = None) -> dict:
        with self._tx() as db:
            job = self._owned(db, job_id, worker)
            span = parse_duration(extend) if extend else job["timeout"]
            self._set(db, job_id, lease_expires=now() + span)
            if progress:
                self._event(db, job_id, "progress", worker, text=progress)
            return self._get(db, job_id)

    def complete(self, job_id: str, worker: str, summary: str, evidence: str | None = None,
                 artifacts=None) -> dict:
        summary = (summary or "").strip()
        if not summary:
            raise HopperError("completing a job needs a summary of what was done")
        with self._tx() as db:
            job = self._owned(db, job_id, worker)
            if self.cfg.policy(job["queue"]).require_evidence and not (evidence or "").strip():
                raise HopperError(f"queue {job['queue']!r} requires evidence: what you ran or "
                                  "checked, and what you saw")
            result = {"summary": summary, "evidence": evidence, "artifacts": _list(artifacts),
                      "by": worker, "at": now()}
            self._finish(db, job, "done", worker, result=result)
            return self._get(db, job_id)

    def fail(self, job_id: str, worker: str, reason: str, retry: bool = True) -> dict:
        with self._tx() as db:
            job = self._owned(db, job_id, worker)
            if retry and job["attempts"] < job["max_attempts"]:
                backoff = min(3600, 30 * 2 ** (job["attempts"] - 1))
                self._set(db, job_id, status="queued", lease_owner=None, lease_expires=None,
                          not_before=now() + backoff)
                self._event(db, job_id, "retry_scheduled", worker, reason=reason, backoff_s=backoff)
            else:
                self._finish(db, job, "failed", worker, reason=reason,
                             result={"summary": reason, "by": worker, "at": now()})
            return self._get(db, job_id)

    def release(self, job_id: str, worker: str, reason: str | None = None) -> dict:
        """Give a job back untouched; the attempt is not counted."""
        with self._tx() as db:
            job = self._owned(db, job_id, worker)
            self._set(db, job_id, status="queued", lease_owner=None, lease_expires=None,
                      attempts=max(0, job["attempts"] - 1))
            self._event(db, job_id, "released", worker, reason=reason)
            return self._get(db, job_id)

    def ask(self, job_id: str, worker: str, question: str) -> dict:
        """Park the job until the requester answers; the worker is free meanwhile."""
        question = (question or "").strip()
        if not question:
            raise HopperError("ask needs a question")
        with self._tx() as db:
            job = self._owned(db, job_id, worker)
            thread = job["thread"] + [{"at": now(), "kind": "question", "by": worker, "text": question}]
            self._set(db, job_id, status="needs_input", thread=thread, lease_owner=None,
                      lease_expires=None, attempts=max(0, job["attempts"] - 1))
            self._event(db, job_id, "asked", worker)
            job = self._get(db, job_id)
            self._notify(db, job, "needs_input", question)
            return job

    def split(self, job_id: str, worker: str, children: list[dict]) -> dict:
        """Create child jobs and park the parent until they all finish.

        Each child is a dict of add() fields. A child's depends_on may name earlier
        children by position, as '#0', '#1', ... to order them.
        """
        if not children:
            raise HopperError("split needs at least one child")
        allowed = {"title", "body", "queue", "priority", "requires", "tags", "source", "key",
                   "depends_on", "not_before", "max_attempts", "timeout", "done_when", "meta"}
        with self._tx() as db:  # all children or none
            job = self._owned(db, job_id, worker)
            created: list[str] = []
            for i, spec in enumerate(children):
                spec = dict(spec)
                unknown = set(spec) - allowed
                if unknown:
                    raise HopperError(f"child {i}: unknown field(s) {', '.join(sorted(unknown))}")
                if not spec.get("title"):
                    raise HopperError(f"child {i} needs a title")
                deps = []
                for d in _list(spec.pop("depends_on", None)):
                    if d.startswith("#"):
                        idx = int(d[1:]) if d[1:].isdigit() else -1
                        if not 0 <= idx < i:
                            raise HopperError(f"child {i} can only depend on earlier children (got {d})")
                        deps.append(created[idx])
                    else:
                        deps.append(d)
                spec.setdefault("source", f"split:{job_id}")
                child = self._insert(db, spec.pop("title"), spec.pop("body", ""),
                                     parent_id=job_id, depends_on=deps, actor=worker, **spec)
                created.append(child["id"])
            self._set(db, job_id, status="waiting", lease_owner=None, lease_expires=None,
                      attempts=max(0, job["attempts"] - 1))
            self._event(db, job_id, "split", worker, children=created)
            return self.show(job_id, _db=db)

    def wait(self, job_id: str, worker: str) -> dict:
        """Park a job whose children were added separately until they finish."""
        with self._tx() as db:
            self._owned(db, job_id, worker)
            (open_children,) = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE parent_id = ? AND status IN "
                "('queued','running','needs_input','waiting')", (job_id,)).fetchone()
            if not open_children:
                raise HopperError(f"{job_id} has no open children to wait for")
            job = self._get(db, job_id)
            self._set(db, job_id, status="waiting", lease_owner=None, lease_expires=None,
                      attempts=max(0, job["attempts"] - 1))
            self._event(db, job_id, "waiting", worker, children=open_children)
            return self._get(db, job_id)

    # ── reading ────────────────────────────────────────────────────────────────

    def show(self, job_id: str, _db=None) -> dict:
        db = _db or self._db
        job = self._get(db, job_id)
        job["depends_on"] = [r[0] for r in db.execute(
            "SELECT depends_on FROM deps WHERE job_id = ?", (job_id,))]
        job["children"] = [
            {"id": c["id"], "title": c["title"], "status": c["status"],
             "result": json.loads(c["result"]) if c["result"] else None}
            for c in db.execute("SELECT id, title, status, result FROM jobs WHERE parent_id = ? "
                                "ORDER BY created_at", (job_id,))]
        return job

    def list(self, status="open", queue=None, tags=None, source=None, limit: int = 50) -> list[dict]:
        where, params = [], []
        if status in (None, "", "all"):
            pass
        elif status == "open":
            where.append(f"status IN ({','.join('?' * len(OPEN_STATES))})")
            params += OPEN_STATES
        else:
            states = _list(status)
            bad = [s for s in states if s not in ALL_STATES]
            if bad:
                raise HopperError(f"unknown status {bad[0]!r}; use open, all, or one of {', '.join(ALL_STATES)}")
            where.append(f"status IN ({','.join('?' * len(states))})")
            params += states
        if queue:
            where.append("queue = ?")
            params.append(queue)
        if source:
            where.append("source = ?")
            params.append(source)
        for tag in _list(tags):
            where.append("EXISTS (SELECT 1 FROM json_each(tags) g WHERE g.value = ?)")
            params.append(tag)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Running first, then what would be claimed next.
        sql += (" ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'needs_input' THEN 1"
                " WHEN 'queued' THEN 2 WHEN 'waiting' THEN 3 ELSE 4 END,"
                " priority DESC, created_at LIMIT ?")
        params.append(int(limit))
        return [_row(r) for r in self._db.execute(sql, params).fetchall()]

    def events(self, job_id: str) -> list[dict]:
        rows = self._db.execute("SELECT at, kind, actor, data FROM events WHERE job_id = ? "
                                "ORDER BY id", (job_id,)).fetchall()
        return [{"at": r["at"], "kind": r["kind"], "actor": r["actor"],
                 **(json.loads(r["data"]) if r["data"] else {})} for r in rows]

    def stats(self) -> dict:
        with self._tx() as db:
            reaped = self._reap(db)
        out = {"queues": {}, "reaped": reaped}
        for r in self._db.execute("SELECT queue, status, COUNT(*) n FROM jobs GROUP BY queue, status"):
            out["queues"].setdefault(r["queue"], {})[r["status"]] = r["n"]
        oldest = self._db.execute(
            "SELECT MIN(created_at) FROM jobs WHERE status = 'queued' AND "
            "(not_before IS NULL OR not_before <= ?)", (now(),)).fetchone()[0]
        out["oldest_queued_s"] = round(now() - oldest) if oldest else None
        out["outbox_pending"] = self._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL AND attempts < ?",
            (_OUTBOX_GIVE_UP,)).fetchone()[0]
        out["outbox_stuck"] = self._db.execute(
            "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL AND attempts >= ?",
            (_OUTBOX_GIVE_UP,)).fetchone()[0]
        return out

    # ── housekeeping ───────────────────────────────────────────────────────────

    def flush(self, limit: int = 50) -> dict:
        """Deliver due notices. Failures back off and retry; nothing is lost."""
        due = self._db.execute(
            "SELECT * FROM outbox WHERE delivered_at IS NULL AND next_at <= ? AND attempts < ? "
            "ORDER BY id LIMIT ?", (now(), _OUTBOX_GIVE_UP, limit)).fetchall()
        sent = failed = 0
        for r in due:
            try:
                sinks.deliver(r["target"], json.loads(r["payload"]), self.cfg)
            except Exception as exc:  # any sink failure is recorded and retried
                failed += 1
                backoff = min(3600, 15 * 2 ** r["attempts"])
                self._db.execute("UPDATE outbox SET attempts = attempts + 1, next_at = ?, "
                                 "last_error = ? WHERE id = ?",
                                 (now() + backoff, str(exc)[:500], r["id"]))
            else:
                sent += 1
                self._db.execute("UPDATE outbox SET delivered_at = ?, attempts = attempts + 1 "
                                 "WHERE id = ?", (now(), r["id"]))
        return {"sent": sent, "failed": failed}

    def gc(self, days: float | None = None) -> dict:
        """Trim finished history older than `days` (default: config history_days)."""
        cutoff = now() - 86400 * (self.cfg.history_days if days is None else float(days))
        with self._tx() as db:
            ids = [r[0] for r in db.execute(
                f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(TERMINAL_STATES))}) "
                "AND finished_at < ? AND id NOT IN (SELECT parent_id FROM jobs WHERE "
                "parent_id IS NOT NULL AND status IN ('queued','running','needs_input','waiting'))",
                (*TERMINAL_STATES, cutoff))]
            for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
                marks = ",".join("?" * len(chunk))
                db.execute(f"DELETE FROM events WHERE job_id IN ({marks})", chunk)
                db.execute(f"DELETE FROM deps WHERE job_id IN ({marks}) OR depends_on IN ({marks})",
                           chunk + chunk)
                db.execute(f"DELETE FROM outbox WHERE job_id IN ({marks})", chunk)
                db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", chunk)
            db.execute("DELETE FROM outbox WHERE delivered_at IS NOT NULL AND delivered_at < ?",
                       (cutoff,))
        return {"removed": len(ids)}
