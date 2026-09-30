"""`hop mcp`: Hopper as MCP tools over stdio, for any MCP-capable agent harness.

Implements the small part of the Model Context Protocol a tool server needs
(initialize, tools/list, tools/call, ping) with the standard library, so installing
Hopper pulls in nothing else. Talks to a local database or, with HOPPER_URL set, to a
remote `hop serve`.
"""
from __future__ import annotations

import json
import os
import socket
import sys

from . import __version__
from .client import connect
from .prompt import render
from .util import HopperError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_S = {"type": "string"}
_ARR = {"type": "array", "items": {"type": "string"}}
_JOB = {"job_id": {**_S, "description": "Job id, e.g. j_01J..."}}

TOOLS = [
    {
        "name": "hop_add",
        "description": "Queue a job for an agent to do. Give a clear title, full instructions in body, "
                       "and done_when (how to tell it's finished). Priority: urgent (100), high (70), "
                       "normal (50), low (20), background (10), idle (5), or 0-100. A key makes it "
                       "idempotent: re-adding the same key while it's open returns the existing job.",
        "inputSchema": {"type": "object", "required": ["title"], "properties": {
            "title": _S, "body": _S, "done_when": _S,
            "priority": {"type": ["string", "integer"]}, "queue": _S,
            "requires": {**_ARR, "description": "Capabilities a worker needs, e.g. [\"shell\", \"git\"]"},
            "tags": _ARR, "reply_to": {**_S, "description": "Where to deliver the outcome, e.g. telegram:<chat>"},
            "key": _S, "depends_on": _ARR,
            "not_before": {**_S, "description": "Earliest start: ISO time, epoch, or +10m"},
            "timeout": {**_S, "description": "Lease length without a heartbeat, e.g. 15m"},
            "max_attempts": {"type": "integer"}}},
    },
    {
        "name": "hop_list",
        "description": "List jobs. status: open (default), all, or queued/running/needs_input/waiting/done/failed/cancelled.",
        "inputSchema": {"type": "object", "properties": {
            "status": _S, "queue": _S, "tags": _ARR, "limit": {"type": "integer"}}},
    },
    {
        "name": "hop_show",
        "description": "Everything about one job: instructions, thread, sub-jobs and their results, outcome, history.",
        "inputSchema": {"type": "object", "required": ["job_id"], "properties": {**_JOB}},
    },
    {
        "name": "hop_claim",
        "description": "Take the most important job you can do. Returns the job and a 'brief' to work from, "
                       "or null if nothing is waiting. You hold it until you complete, fail, ask, split "
                       "or release it (or your lease runs out).",
        "inputSchema": {"type": "object", "properties": {
            "caps": {**_ARR, "description": "Capabilities you have, e.g. [\"shell\", \"git\", \"web\"]"},
            "queues": _ARR, "min_priority": {"type": ["string", "integer"]}, "tags": _ARR}},
    },
    {
        "name": "hop_heartbeat",
        "description": "Keep your claim on a long job alive, optionally noting progress.",
        "inputSchema": {"type": "object", "required": ["job_id"], "properties": {
            **_JOB, "progress": _S, "extend": _S}},
    },
    {
        "name": "hop_complete",
        "description": "Mark your job done. summary: what you did or found. evidence: what you ran or checked "
                       "just now and what you saw (not assumptions, not old logs).",
        "inputSchema": {"type": "object", "required": ["job_id", "summary"], "properties": {
            **_JOB, "summary": _S, "evidence": _S, "artifacts": _ARR}},
    },
    {
        "name": "hop_fail",
        "description": "Give up on your job. retry=true (default) re-queues it with backoff if attempts remain.",
        "inputSchema": {"type": "object", "required": ["job_id", "reason"], "properties": {
            **_JOB, "reason": _S, "retry": {"type": "boolean"}}},
    },
    {
        "name": "hop_release",
        "description": "Hand your job back untouched (the attempt isn't counted), e.g. it needs something you lack.",
        "inputSchema": {"type": "object", "required": ["job_id"], "properties": {**_JOB, "reason": _S}},
    },
    {
        "name": "hop_ask",
        "description": "Ask the requester a question. The job waits for the answer and returns to the queue "
                       "with it; you're free to do other work meanwhile.",
        "inputSchema": {"type": "object", "required": ["job_id", "question"], "properties": {
            **_JOB, "question": _S}},
    },
    {
        "name": "hop_answer",
        "description": "Answer a job's pending question; the job goes back to the queue.",
        "inputSchema": {"type": "object", "required": ["job_id", "text"], "properties": {**_JOB, "text": _S}},
    },
    {
        "name": "hop_split",
        "description": "Split your job into sub-jobs for other workers. Each child: {title, body, done_when, "
                       "requires, priority, depends_on}. depends_on may name earlier children as '#0', '#1'. "
                       "Your job waits and comes back with every child's result.",
        "inputSchema": {"type": "object", "required": ["job_id", "children"], "properties": {
            **_JOB, "children": {"type": "array", "items": {"type": "object", "required": ["title"]}}}},
    },
    {
        "name": "hop_cancel",
        "description": "Cancel a job (and its open sub-jobs).",
        "inputSchema": {"type": "object", "required": ["job_id"], "properties": {**_JOB, "reason": _S}},
    },
    {
        "name": "hop_bump",
        "description": "Change a job's priority.",
        "inputSchema": {"type": "object", "required": ["job_id", "priority"], "properties": {
            **_JOB, "priority": {"type": ["string", "integer"]}}},
    },
    {
        "name": "hop_stats",
        "description": "Queue counts by status, oldest waiting job, and undelivered notices.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _worker_name() -> str:
    return os.environ.get("HOPPER_WORKER") or f"mcp-{socket.gethostname()}-{os.getpid()}"


def _call(hop, name: str, args: dict, worker: str):
    a = dict(args)
    if name == "hop_add":
        return hop.add(**a)
    if name == "hop_list":
        return hop.list(**a)
    if name == "hop_show":
        job = hop.show(job_id=a["job_id"])
        job["history"] = hop.events(job_id=a["job_id"])
        return job
    if name == "hop_claim":
        job = hop.claim(worker=worker, **a)
        if job:
            job["brief"] = render(job, events=hop.events(job_id=job["id"]))
        return job
    if name == "hop_heartbeat":
        return hop.heartbeat(worker=worker, **a)
    if name == "hop_complete":
        return hop.complete(worker=worker, **a)
    if name == "hop_fail":
        return hop.fail(worker=worker, **a)
    if name == "hop_release":
        return hop.release(worker=worker, **a)
    if name == "hop_ask":
        return hop.ask(worker=worker, **a)
    if name == "hop_answer":
        return hop.answer(**a)
    if name == "hop_split":
        return hop.split(worker=worker, **a)
    if name == "hop_cancel":
        return hop.cancel(**a)
    if name == "hop_bump":
        return hop.bump(**a)
    if name == "hop_stats":
        return hop.stats()
    raise HopperError(f"unknown tool {name}")


def run(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    hop = connect()
    worker = _worker_name()

    def reply(msg_id, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        stdout.write(json.dumps(msg, default=str) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if msg_id is None:  # notifications (e.g. notifications/initialized) need no reply
            continue
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            reply(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hopper", "version": __version__},
                "instructions": "Hopper is a shared job queue. Use hop_add to hand off work, "
                                "hop_claim to take work, and always finish a claimed job with "
                                "hop_complete (with evidence), hop_ask, hop_split or hop_fail.",
            })
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            try:
                result = _call(hop, name, params.get("arguments") or {}, worker)
                reply(msg_id, {"content": [{"type": "text",
                                            "text": json.dumps(result, indent=1, default=str)}]})
            except (HopperError, TypeError, KeyError) as exc:
                reply(msg_id, {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})
        else:
            reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
