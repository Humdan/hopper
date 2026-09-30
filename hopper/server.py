"""`hop serve`: the queue over HTTP, for other machines, apps and harnesses.

POST /v1/<op>   JSON body = the operation's arguments   -> {"ok": true, "result": ...}
GET  /v1/jobs[?status=&queue=&limit=]                   -> list
GET  /v1/jobs/<id>                                      -> show
GET  /health                                            -> {"ok": true}

Auth is a bearer token from [tokens] in the config; each token has scopes. The server
binds 127.0.0.1 unless told otherwise, and refuses to run without tokens on any other
address.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .api import ACTOR_OPS, OPS, WORKER_OPS, allowed
from .config import resolve_secret
from .core import Hopper
from .util import HopperError

MAX_BODY = 1 << 20  # 1 MiB is plenty for a job


def serve(hop: Hopper, host: str = "127.0.0.1", port: int = 8770, no_auth: bool = False,
          housekeeping_s: float = 10.0, quiet: bool = False) -> None:
    tokens = []
    for t in hop.cfg.tokens:
        secret = resolve_secret(t.secret)
        if not secret or len(secret) < 16:
            raise HopperError(f"token {t.name!r} needs a secret of at least 16 characters")
        tokens.append((t.name, secret.encode(), t.scopes))
    if not tokens and not no_auth:
        raise HopperError("no [tokens] in the config; add one, or pass --no-auth (localhost only)")
    if no_auth and host not in ("127.0.0.1", "localhost", "::1"):
        raise HopperError("--no-auth is only allowed on localhost")

    lock = threading.Lock()  # one SQLite connection; calls are short

    def authenticate(header: str):
        if no_auth and not header:
            return ("local", ("admin",))
        if not header.startswith("Bearer "):
            return None
        given = header[7:].strip().encode()
        for name, secret, scopes in tokens:
            if hmac.compare_digest(given, secret):
                return (name, scopes)
        return None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"hopper/{__version__}"

        def log_message(self, fmt, *args):
            if not quiet:
                super().log_message(fmt, *args)

        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _call(self, op: str, args: dict) -> None:
            who = authenticate(self.headers.get("Authorization", ""))
            if who is None:
                return self._send(401, {"ok": False, "error": "missing or wrong bearer token"})
            name, scopes = who
            if op not in OPS:
                return self._send(404, {"ok": False, "error": f"no operation {op!r}"})
            if not allowed(scopes, op):
                return self._send(403, {"ok": False, "error": f"token {name!r} may not {op}"})
            if not isinstance(args, dict):
                return self._send(400, {"ok": False, "error": "body must be a JSON object"})
            args = {k: v for k, v in args.items() if not k.startswith("_")}  # internals stay internal
            if op in WORKER_OPS:
                # A token can only act as its own workers.
                w = str(args.get("worker") or "worker")
                args["worker"] = w if w.startswith(f"{name}/") else f"{name}/{w}"
            if op in ACTOR_OPS:
                args["actor"] = name
            if op == "add" and not args.get("source"):
                args["source"] = name
            try:
                with lock:
                    result = getattr(hop, op)(**args)
            except HopperError as exc:
                return self._send(409, {"ok": False, "error": str(exc)})
            except TypeError as exc:
                return self._send(400, {"ok": False, "error": f"bad arguments for {op}: {exc}"})
            self._send(200, {"ok": True, "result": result})

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/health":
                return self._send(200, {"ok": True, "version": __version__})
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path == "/v1/jobs":
                args = {k: q[k] for k in ("status", "queue", "tags", "source") if k in q}
                if "limit" in q:
                    args["limit"] = int(q["limit"])
                return self._call("list", args)
            if url.path.startswith("/v1/jobs/"):
                return self._call("show", {"job_id": url.path.rsplit("/", 1)[1]})
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"ok": False, "error": "body too large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                args = json.loads(raw or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "body is not JSON"})
            path = urllib.parse.urlparse(self.path).path
            if path == "/v1/jobs":
                return self._call("add", args)
            if path.startswith("/v1/"):
                return self._call(path[4:], args)
            self._send(404, {"ok": False, "error": "not found"})

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    stop = threading.Event()

    def housekeeping():
        while not stop.wait(housekeeping_s):
            try:
                with lock:
                    hop.stats()  # reaps expired leases
                    hop.flush()
            except Exception as exc:  # keep serving; report and try again next tick
                print(f"hopper: housekeeping error: {exc}", flush=True)

    threading.Thread(target=housekeeping, daemon=True).start()
    if not quiet:
        auth = "no auth (localhost)" if no_auth else f"{len(tokens)} token(s)"
        print(f"hopper {__version__} serving {hop.db_path} on http://{host}:{port} ({auth})",
              flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        time.sleep(0)
