#!/usr/bin/env python3
"""`hop pulse`: the Claude Code hook that shows a session on the Hopper dashboard (presence + what it's doing).

`hop setup client` registers it for SessionStart, UserPromptSubmit, PostToolUse, Stop and SessionEnd.
It never blocks or fails the session: any error is swallowed, the call times out in 2 s, and tool-use
pings are throttled to one every 20 s.

Where it reports: HOPPER_URL + HOPPER_TOKEN if set, else the cluster this device joined (role.json),
else the local database if this device is a mainframe. HOPPER_SESSION_NAME overrides the session name
(default: claude:<project folder>).
"""
import json, os, socket, sys, time, urllib.request

THROTTLE_S = 20


def _load_env() -> None:
    """HOPPER_URL / HOPPER_TOKEN from the joined cluster, or the older pulse.env, when not already set."""
    from ..cluster import config_dir, load_role
    client = load_role().get("client")
    if client:
        os.environ.setdefault("HOPPER_URL", client["url"])
        os.environ.setdefault("HOPPER_TOKEN", client["token"])
    try:
        for line in open(os.path.join(config_dir(), "pulse.env")):
            k, _, v = line.strip().partition("=")
            if k.startswith("HOPPER_") and v:
                os.environ.setdefault(k, v.strip("\"'"))
    except OSError:
        pass


def main() -> None:
    _load_env()
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return
    kind = ev.get("hook_event_name", "")
    host = socket.gethostname().split(".")[0].lower()
    proj = os.path.basename((ev.get("cwd") or os.getcwd()).rstrip("/")) or "home"
    url, token = os.environ.get("HOPPER_URL"), os.environ.get("HOPPER_TOKEN")
    # over HTTP the server already prefixes the token's name (which is the machine), so leave the host out
    sid = (ev.get("session_id") or "x")[:8]
    stamp = f"/tmp/hop-pulse-{sid}"
    # One session, one name: pin it to the folder the session started in, so `cd` mid-session
    # does not make a single window show up as several.
    namefile = f"/tmp/hop-pulse-name-{sid}"
    try:
        proj = open(namefile).read().strip() or proj
    except OSError:
        try:
            open(namefile, "w").write(proj)
        except OSError:
            pass
    name = os.environ.get("HOPPER_SESSION_NAME") or (f"claude:{proj}" if url else f"{host}/claude:{proj}")
    args = {"worker": name, "kind": "session"}
    if kind == "SessionEnd":
        args["end"] = True
        for f in (namefile, stamp):
            try:
                os.remove(f)
            except OSError:
                pass
    elif kind == "Stop":
        args["doing"] = "idle"
    elif kind == "SessionStart":
        args["doing"] = "idle"
        args["pulse"] = True
    elif kind == "UserPromptSubmit":
        args["doing"] = "working: " + " ".join(str(ev.get("prompt", "")).split())[:120]
        args["pulse"] = True
    elif kind == "PostToolUse":
        try:
            if time.time() - os.path.getmtime(stamp) < THROTTLE_S:
                return
        except OSError:
            pass
        tool, inp = ev.get("tool_name", ""), ev.get("tool_input") or {}
        what = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("description") or ""
        args["doing"] = f"{tool} {' '.join(str(what).split())[:100]}".strip()
        open(stamp, "w").close()
    else:
        return
    if url:
        req = urllib.request.Request(url.rstrip("/") + "/v1/ping", data=json.dumps(args).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        urllib.request.urlopen(req, timeout=2).read()
    else:
        from ..cluster import load_role
        if not load_role().get("mainframe"):
            return
        from ..core import Hopper
        Hopper().ping(**args)


def run() -> int:
    try:
        main()
    except Exception:
        pass
    return 0
