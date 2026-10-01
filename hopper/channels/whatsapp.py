"""WhatsApp channel, through Meta's official WhatsApp Business Cloud API.

Unlike Telegram, WhatsApp pushes messages to a webhook, so this needs:
  - a Meta developer app with the WhatsApp product and a phone number
    (token, phone_number_id, and the app secret for signature checks)
  - a public HTTPS URL that reaches `listen` here (e.g. Tailscale Funnel or a tunnel),
    registered as the app's webhook with the same verify_token

Limits worth knowing: messages can't be edited (replies arrive whole), and outside 24
hours from the person's last message WhatsApp only allows pre-approved templates, so a
notification sent then may be refused.

Unofficial "WhatsApp Web" libraries are deliberately not used: they break WhatsApp's
terms and get numbers banned.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..util import HopperError, expand
from . import Channel, Message, file_kind, multipart
from .telegram import chunks

GRAPH = "https://graph.facebook.com/v21.0"


class WhatsApp(Channel):
    name = "whatsapp"
    can_edit = False

    def __init__(self, conf: dict):
        super().__init__(conf)
        self.token = self.secret("token")
        self.phone_id = str(conf.get("phone_number_id") or "")
        if not self.phone_id:
            raise HopperError("[channels.whatsapp] needs phone_number_id")
        self.files_dir = expand(conf.get("files_dir") or "~/.local/share/hopper/inbox/whatsapp")

    # -- Graph API ------------------------------------------------------------
    def _request(self, url: str, body: bytes | None = None, ctype: str = "application/json",
                 timeout: float = 30):
        req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {self.token}",
                                                              **({"Content-Type": ctype} if body else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
        except urllib.error.HTTPError as exc:
            try:
                reason = json.load(exc).get("error", {}).get("message", "")
            except Exception:
                reason = ""
            raise HopperError(f"whatsapp: HTTP {exc.code} {reason}".strip()) from None
        except urllib.error.URLError as exc:
            raise HopperError(f"whatsapp unreachable: {exc.reason}") from None

    def _message(self, chat: str, payload: dict) -> str:
        body = json.dumps({"messaging_product": "whatsapp", "to": chat, **payload}).encode()
        data = self._request(f"{GRAPH}/{self.phone_id}/messages", body)
        return str((data.get("messages") or [{}])[0].get("id", ""))

    # -- interface ------------------------------------------------------------
    def send(self, chat: str, text: str) -> str:
        last = ""
        for part in chunks(text, 4000):
            last = self._message(chat, {"type": "text", "text": {"body": part or "…"}})
        return last

    def send_file(self, chat: str, path: str, caption: str = "") -> str:
        kind = {"image": "image", "voice": "audio", "video": "video"}.get(file_kind(path), "document")
        body, ctype = multipart({"messaging_product": "whatsapp"}, "file", path)
        media = self._request(f"{GRAPH}/{self.phone_id}/media", body, ctype, timeout=120)
        obj = {"id": media["id"]}
        if caption and kind != "audio":
            obj["caption"] = caption[:1000]
        if kind == "document":
            obj["filename"] = os.path.basename(path)
        return self._message(chat, {"type": kind, kind: obj})

    def _download(self, media_id: str, name_hint: str) -> str | None:
        try:
            info = self._request(f"{GRAPH}/{media_id}")
            data = self._request(info["url"], timeout=60)
            os.makedirs(self.files_dir, exist_ok=True)
            path = os.path.join(self.files_dir, f"{int(time.time())}-{name_hint}")
            with open(path, "wb") as fh:
                fh.write(data if isinstance(data, bytes) else json.dumps(data).encode())
            return path
        except Exception as exc:
            print(f"whatsapp: couldn't download attachment: {type(exc).__name__}", file=sys.stderr)
            return None

    def parse(self, payload: dict) -> list[Message]:
        """Messages from one webhook delivery (statuses and other events are ignored)."""
        out = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                for m in (change.get("value") or {}).get("messages") or []:
                    sender = str(m.get("from", ""))
                    if not self.allowed(sender):
                        print(f"whatsapp: dropped a message from {sender} (not in allow)", file=sys.stderr)
                        continue
                    typ = m.get("type")
                    text, files = "", []
                    if typ == "text":
                        text = (m.get("text") or {}).get("body", "")
                    elif typ in ("image", "audio", "video", "document", "sticker"):
                        media = m.get(typ) or {}
                        text = media.get("caption", "")
                        ext = {"image": "jpg", "audio": "ogg", "video": "mp4", "sticker": "webp"}.get(typ, "bin")
                        p = self._download(media.get("id", ""), media.get("filename") or f"{typ}.{ext}")
                        files += [p] if p else []
                    if text or files:
                        out.append(Message("whatsapp", sender, sender, text, str(m.get("id", "")), files))
        return out

    def receive(self, on_message) -> None:
        """Serve the webhook: GET answers Meta's verification, POST carries messages."""
        listen = str(self.conf.get("listen") or "127.0.0.1:8771")
        host, _, port = listen.rpartition(":")
        verify = self.secret("verify_token")
        app_secret = self.secret("app_secret").encode()
        channel = self

        class Hook(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code: int, body: bytes = b"") -> None:
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                q = parse_qs(urlparse(self.path).query)
                if (q.get("hub.mode") == ["subscribe"]
                        and hmac.compare_digest((q.get("hub.verify_token") or [""])[0], verify)):
                    return self._reply(200, (q.get("hub.challenge") or [""])[0].encode())
                self._reply(403)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                sig = self.headers.get("X-Hub-Signature-256", "")
                good = "sha256=" + hmac.new(app_secret, raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig, good):
                    return self._reply(401)       # not from Meta: drop it
                self._reply(200)                  # ack fast; Meta retries slow webhooks
                try:
                    msgs = channel.parse(json.loads(raw))
                except ValueError:
                    return
                for msg in msgs:
                    threading.Thread(target=on_message, args=(msg,), daemon=True).start()

        server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Hook)
        print(f"whatsapp: webhook listening on {listen}", file=sys.stderr)
        server.serve_forever()
