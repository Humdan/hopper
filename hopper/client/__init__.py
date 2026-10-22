"""A client: any device that uses a cluster's mainframe (its agents show up on the dashboard and share the queue)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from .. import cluster
from .remote import RemoteHopper, connect  # noqa: F401  (the HTTP client lives here too)
from ..util import HopperError

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")


def _http(url: str, token: str | None = None, body: dict | None = None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise HopperError(f"the mainframe answered HTTP {exc.code} for {url}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HopperError(f"can't reach the mainframe at {url}: {getattr(exc, 'reason', exc)}") from None


def join(code: str, agents: bool = True) -> dict:
    """Become a client of the mainframe that issued this join code."""
    data = cluster.decode_join(code)
    role = cluster.load_role()
    current = role.get("client")
    if current and current["id"] != data["id"]:
        raise HopperError(f"this device is already a client of cluster {current['cluster']!r}. "
                          "Run `hop setup client --leave` first; a device belongs to one cluster as a client.")
    health = _http(data["url"].rstrip("/") + "/health")
    if health.get("cluster_id") != data["id"]:
        raise HopperError(f"the server at {data['url']} is not the mainframe this code was made for "
                          "(its cluster id differs). A cluster has exactly one mainframe.")
    _http(data["url"].rstrip("/") + "/v1/stats", data["token"], {})   # proves the token works
    mf = role.get("mainframe")
    role["client"] = {"cluster": data["cluster"], "id": data["id"], "url": data["url"].rstrip("/"),
                      "token": data["token"], "name": data["name"],
                      "self": bool(mf and mf["id"] == data["id"])}
    cluster.save_role(role)
    return {**role["client"], "token": "…", "agents": install_agents() if agents else None}


def leave() -> bool:
    role = cluster.load_role()
    if not role.pop("client", None):
        return False
    cluster.save_role(role)
    remove_agents()
    return True


def _settings_path() -> str:
    return os.path.expanduser("~/.claude/settings.json")


def _hook_command() -> str:
    return f"{shutil.which('hop') or 'hop'} pulse"


def install_hooks() -> str:
    """Register `hop pulse` in Claude Code's user settings (backs the file up first)."""
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as fh:
            settings = json.load(fh)
        shutil.copyfile(path, path + ".prehop")
    except OSError:
        settings = {}
    except ValueError:
        raise HopperError(f"{path} isn't valid JSON; fix it or move it aside, then run this again") from None
    hooks = settings.setdefault("hooks", {})
    cmd = {"type": "command", "command": _hook_command(), "timeout": 5}
    for ev in HOOK_EVENTS:
        groups = hooks.setdefault(ev, [])
        groups[:] = [g for g in groups if not _is_ours(g)]   # replace an older copy (e.g. a moved `hop`)
        groups.append({"matcher": "*", "hooks": [cmd]} if ev == "PostToolUse" else {"hooks": [cmd]})
    with open(path, "w") as fh:
        json.dump(settings, fh, indent=2)
    return path


def _is_ours(group: dict) -> bool:
    return any(str(c.get("command", "")).endswith(" pulse") and "hop" in str(c.get("command", ""))
               or str(c.get("command", "")).endswith("hop-pulse") for c in group.get("hooks", []))


def remove_hooks() -> None:
    path = _settings_path()
    try:
        with open(path) as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return
    hooks = settings.get("hooks") or {}
    for ev in list(hooks):
        hooks[ev] = [g for g in hooks[ev] if not _is_ours(g)]
        if not hooks[ev]:
            del hooks[ev]
    with open(path, "w") as fh:
        json.dump(settings, fh, indent=2)


def _hop() -> str:
    return shutil.which("hop") or "hop"


def _claude(*args: str) -> subprocess.CompletedProcess | None:
    claude = shutil.which("claude")
    if not claude:
        return None
    try:
        return subprocess.run([claude, *args], capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return None


def install_skill() -> str:
    """Teach Claude Code what Hopper is and how to use it (a user-level skill)."""
    from importlib import resources
    dest = os.path.expanduser("~/.claude/skills/hopper/SKILL.md")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as fh:
        fh.write(resources.files("hopper.client").joinpath("SKILL.md").read_text(encoding="utf-8"))
    return dest


def install_mcp() -> str:
    """Give Claude Code the hop_* tools. `hop mcp` reads this device's cluster membership, so no token goes in the config."""
    if _claude("--version") is None:
        return "skipped: the `claude` command isn't on PATH. Run later: claude mcp add --scope user hopper -- hop mcp"
    _claude("mcp", "remove", "hopper", "--scope", "user")   # replace an older entry, e.g. a moved `hop`
    r = _claude("mcp", "add", "--scope", "user", "hopper", "--", _hop(), "mcp")
    if r is None or r.returncode != 0:
        return f"failed: {(r.stderr or r.stdout).strip()[:160] if r else 'timed out'}"
    return "added to Claude Code (user scope)"


def install_agents() -> dict:
    """Everything Claude Code needs to work with this cluster: the presence hook, the tools and the skill."""
    return {"hooks": install_hooks(), "skill": install_skill(), "mcp": install_mcp()}


def remove_agents() -> None:
    remove_hooks()
    shutil.rmtree(os.path.expanduser("~/.claude/skills/hopper"), ignore_errors=True)
    _claude("mcp", "remove", "hopper", "--scope", "user")
