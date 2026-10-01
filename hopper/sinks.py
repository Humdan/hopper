"""Delivering notices to a job's reply_to address.

Jobs can arrive from remote producers, so a reply_to address is never trusted to run
arbitrary things: hooks are named in the user's config, files stay under one directory,
and Telegram chats and webhook URLs must be allow-listed.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .config import Config
from .util import HopperError

SCHEMES = ("stdout", "file", "webhook", "telegram", "whatsapp", "hook")


def validate(reply_to: str | None, cfg: Config) -> str | None:
    """Reject an address at add time instead of failing silently at delivery time."""
    if not reply_to:
        return None
    scheme, _, rest = reply_to.partition(":")
    if scheme not in SCHEMES:
        raise HopperError(f"reply_to scheme must be one of {', '.join(SCHEMES)} (got {reply_to!r})")
    if scheme == "file":
        _file_path(rest, cfg)
    elif scheme == "webhook":
        if not rest.startswith(("https://", "http://")):
            raise HopperError("webhook reply_to must be webhook:https://...")
        if not any(rest.startswith(p) for p in cfg.webhook_allow):
            raise HopperError("webhook URL is not allowed; add its prefix to [sinks.webhook] allow")
    elif scheme in ("telegram", "whatsapp"):
        allowed = set(cfg.telegram_chats if scheme == "telegram" else ())
        allowed |= {str(c) for c in (cfg.channels.get(scheme) or {}).get("allow") or ()}
        if rest not in allowed:
            raise HopperError(f"{scheme} chat is not allowed; add it to [channels.{scheme}] allow")
    elif scheme == "hook":
        if rest not in cfg.hooks:
            raise HopperError(f"no hook named {rest!r} in [hooks]")
    return reply_to


def _file_path(rest: str, cfg: Config) -> str:
    """file:<name>.jsonl, or an absolute path, but always inside cfg.file_dir."""
    base = cfg.file_dir
    path = os.path.abspath(rest if os.path.isabs(rest) else os.path.join(base, rest))
    if os.path.commonpath([path, base]) != base:
        raise HopperError(f"file reply_to must be inside {base}")
    return path


def render_text(notice: dict) -> str:
    """A short human message for chat-style sinks."""
    job = notice["job"]
    head = {
        "done": "Done",
        "failed": "Failed",
        "needs_input": "Question",
        "cancelled": "Cancelled",
    }.get(notice["event"], notice["event"])
    lines = [f"{head}: {job['title']}"]
    if notice.get("text"):
        lines.append(notice["text"])
    lines.append(f"({job['id']})")
    return "\n".join(lines)


def deliver(target: str, notice: dict, cfg: Config) -> None:
    """Deliver one notice. Raises on failure so the outbox can retry."""
    scheme, _, rest = target.partition(":")
    validate(target, cfg)  # config may have changed since the job was added
    if scheme == "stdout":
        print(json.dumps(notice), flush=True)
    elif scheme == "file":
        path = _file_path(rest, cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(notice) + "\n")
    elif scheme == "webhook":
        req = urllib.request.Request(rest, data=json.dumps(notice).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "hopper"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status >= 300:
                raise HopperError(f"webhook answered HTTP {r.status}")
    elif scheme in ("telegram", "whatsapp") and scheme in cfg.channels:
        from . import channels
        files = [p for p in notice.get("artifacts") or [] if os.path.isfile(p)]
        channels.send(cfg, target, render_text(notice), files)
    elif scheme == "telegram":
        token = os.environ.get("HOPPER_TELEGRAM_TOKEN")
        if not token:
            raise HopperError("HOPPER_TELEGRAM_TOKEN is not set")
        body = urllib.parse.urlencode({"chat_id": rest, "text": render_text(notice)[:4000]})
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            with urllib.request.urlopen(url, data=body.encode(), timeout=15) as r:
                if r.status >= 300:
                    raise HopperError(f"telegram answered HTTP {r.status}")
        except urllib.error.HTTPError as exc:
            # Never let the token-bearing URL reach logs or the outbox.
            raise HopperError(f"telegram answered HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise HopperError(f"telegram unreachable: {exc.reason}") from None
    elif scheme == "hook":
        argv = cfg.hooks[rest]
        proc = subprocess.run(argv, input=json.dumps(notice), text=True,
                              capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise HopperError(f"hook {rest} exited {proc.returncode}: {proc.stderr.strip()[:200]}")
