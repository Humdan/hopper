"""Small shared helpers: ids, time parsing, priority names, the default project."""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone

# Crockford base32: sortable, no ambiguous letters.
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PRIORITY_NAMES = {
    "urgent": 100,
    "high": 70,
    "normal": 50,
    "low": 20,
    "background": 10,
    "idle": 5,
}

OPEN_STATES = ("queued", "running", "needs_input", "waiting")
TERMINAL_STATES = ("done", "failed", "cancelled")
ALL_STATES = OPEN_STATES + TERMINAL_STATES


class HopperError(Exception):
    """A request Hopper refuses, with a message meant for the caller."""


def new_id(prefix: str = "j") -> str:
    """Time-sortable id: 48-bit milliseconds + 80 random bits, base32 (26 chars)."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_B32[n & 31])
        n >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


def now() -> float:
    return time.time()


def parse_priority(value) -> int:
    """Accept 0-100 or a band name ('urgent', 'high', ...)."""
    if value is None:
        return PRIORITY_NAMES["normal"]
    if isinstance(value, str):
        v = value.strip().lower()
        if v in PRIORITY_NAMES:
            return PRIORITY_NAMES[v]
        if not v.lstrip("-").isdigit():
            names = ", ".join(PRIORITY_NAMES)
            raise HopperError(f"priority must be 0-100 or one of: {names}")
        value = int(v)
    value = int(value)
    if not 0 <= value <= 100:
        raise HopperError("priority must be between 0 and 100")
    return value


def priority_name(p: int) -> str:
    """The band a number falls in, for display."""
    for name, floor in sorted(PRIORITY_NAMES.items(), key=lambda kv: -kv[1]):
        if p >= floor:
            return name
    return "idle"


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value) -> float:
    """'30s', '10m', '2h', '1d', or a number of seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION.match(str(value))
    if m:
        return float(m.group(1)) * _UNITS[m.group(2).lower()]
    try:
        return float(value)
    except ValueError:
        raise HopperError(f"not a duration: {value!r} (use e.g. 30s, 10m, 2h, 1d)") from None


def parse_when(value) -> float | None:
    """Absolute time from an epoch, an ISO-8601 string, or a relative '+10m' / '10m'."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    rel = s[1:] if s.startswith("+") else s
    if _DURATION.match(rel):
        return now() + parse_duration(rel)
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HopperError(f"not a time: {value!r} (use ISO-8601, an epoch, or +10m)") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # treat naive times as local
    return dt.timestamp()


def parse_since(value) -> float | None:
    """A point in the past: an age like '7d' or '12h' (that long ago), or anything
    parse_when takes. Used for report windows, where '7d' means the last seven days."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    if not s.startswith("+") and _DURATION.match(s):
        return now() - parse_duration(s)
    return parse_when(value)


def default_project(cwd: str | None = None) -> str | None:
    """The project a new job belongs to when none is given: $HOPPER_PROJECT, else the
    name of the git repository the caller is working in, else none."""
    env = os.environ.get("HOPPER_PROJECT")
    if env is not None:
        return env.strip() or None
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip() if out.returncode == 0 else ""
    return os.path.basename(top) or None


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(ts: float | None) -> str:
    """'3m', '2h', '4d' since ts (or until, prefixed 'in ')."""
    if ts is None:
        return "-"
    d = now() - ts
    future = d < 0
    d = abs(d)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= size:
            s = f"{int(d // size)}{unit}"
            break
    else:
        s = f"{int(d)}s"
    return f"in {s}" if future else s


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
