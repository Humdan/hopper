"""One operation table shared by every interface (HTTP, MCP, remote client).

Each operation names the Hopper method it calls and the token scope it needs over HTTP.
"""
from __future__ import annotations

OPS: dict[str, str] = {
    # producing
    "add": "produce",
    "cancel": "produce",
    "bump": "produce",
    "answer": "produce",
    # reading
    "show": "read",
    "list": "read",
    "events": "read",
    "stats": "read",
    "usage": "read",
    "report": "read",
    # working
    "claim": "work",
    "heartbeat": "work",
    "complete": "work",
    "fail": "work",
    "release": "work",
    "ask": "work",
    "split": "work",
    "wait": "work",
    "record_usage": "work",
    # housekeeping
    "retry": "admin",
    "flush": "admin",
    "gc": "admin",
}

# What each token scope may do. Every scope can read; admin can do everything.
SCOPES: dict[str, set[str]] = {
    "read": {"read"},
    "produce": {"read", "produce"},
    "work": {"read", "work", "produce"},  # workers add child jobs and answer threads
    "admin": {"read", "produce", "work", "admin"},
}

# Operations whose `worker` argument identifies the caller (namespaced per token).
WORKER_OPS = {"claim", "heartbeat", "complete", "fail", "release", "ask", "split", "wait",
              "record_usage"}
# Operations that record who did it.
ACTOR_OPS = {"add", "cancel", "bump", "answer", "retry"}


def allowed(token_scopes, op: str) -> bool:
    need = OPS.get(op)
    if need is None:
        return False
    granted = set()
    for s in token_scopes:
        granted |= SCOPES.get(s, set())
    return need in granted
