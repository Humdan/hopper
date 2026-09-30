"""Pick a local or remote Hopper. Both expose the same methods.

With HOPPER_URL set, every call goes to a `hop serve` over HTTP; otherwise the SQLite
database is opened directly. Callers never need to know which.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .api import OPS
from .util import HopperError


class RemoteHopper:
    """A Hopper on another machine (or process), reached through `hop serve`."""

    def __init__(self, url: str, token: str | None = None, timeout: float = 30):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.db_path = self.url

    def _call(self, op: str, **args):
        req = urllib.request.Request(f"{self.url}/v1/{op}", method="POST",
                                     data=json.dumps(args).encode(),
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except ValueError:
                raise HopperError(f"hopper server answered HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise HopperError(f"can't reach hopper at {self.url}: {exc.reason}") from None
        if not payload.get("ok"):
            raise HopperError(payload.get("error") or "hopper server refused the request")
        return payload.get("result")

    def __getattr__(self, op: str):
        if op not in OPS:
            raise AttributeError(op)
        return lambda **args: self._call(op, **args)

    def close(self) -> None:
        pass


def connect(db: str | None = None):
    """The Hopper this process should talk to: remote if HOPPER_URL is set, else local."""
    url = os.environ.get("HOPPER_URL")
    if url and not db:
        return RemoteHopper(url, os.environ.get("HOPPER_TOKEN"))
    from .core import Hopper
    return Hopper(db=db)
