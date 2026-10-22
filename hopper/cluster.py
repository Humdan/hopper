"""What this device is in a cluster: a mainframe (runs the queue), a client (uses one), or both.

A cluster is one mainframe plus any number of clients. The mainframe mints the cluster's id when it
is set up; join codes carry that id, so a client can only ever join the mainframe that issued the
code, and a second mainframe for the same cluster can't exist (it would mint a different id).

State lives in <config dir>/role.json (mode 600) and, for tokens the mainframe hands out, in
<config dir>/tokens.toml (mode 600).
"""
from __future__ import annotations

import base64
import json
import os
import tomllib

from .util import HopperError, expand

CODE_PREFIX = "hop1."


def config_dir() -> str:
    path = os.environ.get("HOPPER_CONFIG")
    if path:
        return os.path.dirname(expand(path))
    return expand(os.path.join(os.environ.get("XDG_CONFIG_HOME") or "~/.config", "hopper"))


def role_path() -> str:
    return os.path.join(config_dir(), "role.json")


def tokens_path() -> str:
    return os.path.join(config_dir(), "tokens.toml")


def load_role() -> dict:
    try:
        with open(role_path()) as fh:
            role = json.load(fh)
        return role if isinstance(role, dict) else {}
    except (OSError, ValueError):
        return {}


def save_role(role: dict) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    path = role_path()
    if not role:
        if os.path.exists(path):
            os.remove(path)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(role, fh, indent=2)
    os.chmod(path, 0o600)


def encode_join(url: str, cluster: str, cluster_id: str, token: str, name: str) -> str:
    raw = json.dumps({"v": 1, "url": url, "cluster": cluster, "id": cluster_id, "token": token, "name": name},
                     separators=(",", ":")).encode()
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_join(code: str) -> dict:
    code = (code or "").strip()
    if not code.startswith(CODE_PREFIX):
        raise HopperError("that doesn't look like a join code (they start with 'hop1.')")
    body = code[len(CODE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        raise HopperError("the join code is damaged; copy it again") from None
    if data.get("v") != 1 or not all(data.get(k) for k in ("url", "cluster", "id", "token", "name")):
        raise HopperError("the join code is from a different Hopper version or is incomplete")
    return data


def add_token(name: str, secret: str, scopes: tuple[str, ...]) -> None:
    """Append a token to tokens.toml, refusing a name that is already taken."""
    os.makedirs(config_dir(), exist_ok=True)
    path = tokens_path()
    existing = read_tokens()
    if name in existing:
        raise HopperError(f"a token named {name!r} already exists; pick another name")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        scope_list = ", ".join(json.dumps(s) for s in scopes)
        fh.write(f"[tokens.{json.dumps(name)}]\nsecret = {json.dumps(secret)}\nscopes = [{scope_list}]\n\n")
    os.chmod(path, 0o600)


def read_tokens(path: str | None = None) -> dict:
    try:
        with open(path or tokens_path(), "rb") as fh:
            return tomllib.load(fh).get("tokens") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}
