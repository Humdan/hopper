"""Configuration: ~/.config/hopper/config.toml plus environment overrides.

Every setting is optional. Secrets are never written in the file itself: a value of
"env:NAME" is read from the environment at load time.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from .util import HopperError, expand


def _default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return expand(os.path.join(base, "hopper", "config.toml"))


def _default_db_path() -> str:
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return expand(os.path.join(base, "hopper", "hopper.db"))


def resolve_secret(value):
    """'env:NAME' -> os.environ['NAME']; anything else unchanged."""
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        secret = os.environ.get(name)
        if not secret:
            raise HopperError(f"config refers to ${name}, which is not set")
        return secret
    return value


@dataclass
class QueuePolicy:
    require_evidence: bool = False
    aging_minutes: float = 30.0   # +1 effective priority per this many minutes waiting
    aging_cap: int = 20           # never more than this much aging bonus


@dataclass
class Token:
    name: str
    secret: str
    scopes: tuple[str, ...]


@dataclass
class Config:
    path: str | None = None
    db: str = field(default_factory=_default_db_path)
    history_days: float = 30.0
    queues: dict[str, QueuePolicy] = field(default_factory=dict)
    tokens: list[Token] = field(default_factory=list)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    telegram_chats: tuple[str, ...] = ()        # chats the telegram sink may message
    webhook_allow: tuple[str, ...] = ()          # URL prefixes the webhook sink may call
    file_dir: str = ""                           # the file sink writes only under here

    def policy(self, queue: str) -> QueuePolicy:
        return self.queues.get(queue) or self.queues.get("*") or QueuePolicy()


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("HOPPER_CONFIG") or _default_config_path()
    raw: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise HopperError(f"can't read config {path}: {exc}") from None
    else:
        path = None

    cfg = Config(path=path)
    cfg.db = expand(os.environ.get("HOPPER_DB") or raw.get("db") or cfg.db)
    cfg.history_days = float(raw.get("history_days", cfg.history_days))
    cfg.file_dir = expand(raw.get("sinks", {}).get("file", {}).get("dir")
                          or os.path.join(os.path.dirname(cfg.db), "notices"))

    for name, q in (raw.get("queues") or {}).items():
        cfg.queues[name] = QueuePolicy(
            require_evidence=bool(q.get("require_evidence", False)),
            aging_minutes=float(q.get("aging_minutes", 30)),
            aging_cap=int(q.get("aging_cap", 20)),
        )

    # Tokens are resolved lazily-safe: a missing env var disables that token with a
    # clear error only when the server starts, not for local CLI use.
    for name, t in (raw.get("tokens") or {}).items():
        cfg.tokens.append(Token(name=name, secret=t.get("secret", ""),
                                scopes=tuple(t.get("scopes") or ("produce",))))

    for name, argv in (raw.get("hooks") or {}).items():
        if isinstance(argv, str):
            argv = [argv]
        if not argv or not all(isinstance(a, str) for a in argv):
            raise HopperError(f"hook {name!r} must be a command list, e.g. [\"/usr/bin/notify\"]")
        cfg.hooks[name] = [expand(argv[0])] + argv[1:]

    sinks = raw.get("sinks") or {}
    cfg.telegram_chats = tuple(str(c) for c in (sinks.get("telegram", {}).get("chats") or ()))
    cfg.webhook_allow = tuple(sinks.get("webhook", {}).get("allow") or ())
    return cfg
