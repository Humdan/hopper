"""Telegram channel: long-polls the Bot API, so it needs no public URL.

Only one program may poll a bot token at a time (Telegram answers 409 to the second),
so a bot used here can't also be polled by another agent. Sending is fine from
anywhere: `hop send` and the reply_to sink share the token without polling.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from ..util import HopperError, expand
from . import Channel, Message, file_kind, multipart

API = "https://api.telegram.org"
LIMIT = 4000            # Telegram's cap is 4096; leave room


def chunks(text: str, size: int = LIMIT) -> list[str]:
    """Split on line breaks where possible so long replies stay readable."""
    text = text or ""
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


class Telegram(Channel):
    name = "telegram"
    can_edit = True

    def __init__(self, conf: dict):
        super().__init__(conf)
        self.token = self.secret("token", required=False) or os.environ.get("HOPPER_TELEGRAM_TOKEN", "")
        if not self.token:
            raise HopperError("[channels.telegram] needs token (or set $HOPPER_TELEGRAM_TOKEN)")
        self.files_dir = expand(conf.get("files_dir") or "~/.local/share/hopper/inbox/telegram")

    # -- Bot API --------------------------------------------------------------
    def call(self, method: str, params: dict | None = None, timeout: float = 20):
        body = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}).encode()
        try:
            with urllib.request.urlopen(f"{API}/bot{self.token}/{method}", data=body, timeout=timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as exc:
            # Never let the token-bearing URL reach logs: report the code and Telegram's reason.
            try:
                reason = json.load(exc).get("description", "")
            except Exception:
                reason = ""
            raise HopperError(f"telegram {method}: HTTP {exc.code} {reason}".strip()) from None
        except urllib.error.URLError as exc:
            raise HopperError(f"telegram unreachable: {exc.reason}") from None
        if not data.get("ok"):
            raise HopperError(f"telegram {method}: {data.get('description', 'not ok')}")
        return data.get("result")

    # -- interface ------------------------------------------------------------
    def send(self, chat: str, text: str) -> str:
        last = ""
        for part in chunks(text):
            msg = self.call("sendMessage", {"chat_id": chat, "text": part or "…"})
            last = str(msg.get("message_id", ""))
        return last

    def send_file(self, chat: str, path: str, caption: str = "") -> str:
        method, field = {"image": ("sendPhoto", "photo"), "voice": ("sendVoice", "voice"),
                         "video": ("sendVideo", "video")}.get(file_kind(path), ("sendDocument", "document"))
        if method == "sendVoice" and not path.lower().endswith((".ogg", ".oga", ".opus")):
            method, field = "sendAudio", "audio"     # Telegram voice notes must be ogg/opus
        if os.path.getsize(path) > 50 * 1024 * 1024:
            raise HopperError(f"{os.path.basename(path)} is over Telegram's 50 MB bot limit")
        body, ctype = multipart({"chat_id": chat, "caption": (caption or None) and caption[:1000]}, field, path)
        req = urllib.request.Request(f"{API}/bot{self.token}/{method}", data=body,
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
        except urllib.error.HTTPError as exc:
            raise HopperError(f"telegram {method}: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise HopperError(f"telegram unreachable: {exc.reason}") from None
        if not data.get("ok"):
            raise HopperError(f"telegram {method}: {data.get('description', 'not ok')}")
        return str(data["result"].get("message_id", ""))

    def edit(self, chat: str, message_id: str, text: str) -> None:
        if not message_id:
            return
        try:
            self.call("editMessageText", {"chat_id": chat, "message_id": message_id,
                                          "text": (text or "…")[:LIMIT]})
        except HopperError as exc:
            if "not modified" not in str(exc):
                raise

    def typing(self, chat: str) -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=5)
        except HopperError:
            pass

    def _download(self, file_id: str, name_hint: str) -> str | None:
        try:
            info = self.call("getFile", {"file_id": file_id})
            os.makedirs(self.files_dir, exist_ok=True)
            path = os.path.join(self.files_dir, f"{int(time.time())}-{os.path.basename(name_hint)}")
            with urllib.request.urlopen(f"{API}/file/bot{self.token}/{info['file_path']}", timeout=60) as r, \
                    open(path, "wb") as fh:
                fh.write(r.read())
            return path
        except Exception as exc:  # an attachment we can't fetch shouldn't drop the text
            print(f"telegram: couldn't download attachment: {type(exc).__name__}", file=sys.stderr)
            return None

    def _to_message(self, update: dict) -> Message | None:
        m = update.get("message") or update.get("edited_message")
        if not m:
            return None
        chat = str(m["chat"]["id"])
        sender = str((m.get("from") or {}).get("id", chat))
        if not (self.allowed(chat) or self.allowed(sender)):
            print(f"telegram: dropped a message from {sender} (not in allow)", file=sys.stderr)
            return None
        text = m.get("text") or m.get("caption") or ""
        files = []
        if m.get("photo"):
            p = self._download(m["photo"][-1]["file_id"], "photo.jpg")   # largest size
            files += [p] if p else []
        for kind, hint in (("voice", "voice.ogg"), ("audio", "audio"), ("video", "video.mp4"),
                           ("video_note", "video_note.mp4"), ("document", "file")):
            d = m.get(kind)
            if d:
                p = self._download(d["file_id"], d.get("file_name") or hint)
                files += [p] if p else []
        if not text and not files:
            return None
        return Message("telegram", chat, sender, text, str(m.get("message_id", "")), files)

    def receive(self, on_message) -> None:
        """Long-poll forever. The offset is persisted so a restart doesn't replay old messages."""
        state = expand(self.conf.get("state_file") or "~/.local/share/hopper/telegram-offset")
        try:
            offset = int(open(state).read().strip())
        except (OSError, ValueError):
            offset = None
        backoff = 1.0
        while True:
            try:
                updates = self.call("getUpdates", {"timeout": 50, "offset": offset,
                                                   "allowed_updates": '["message","edited_message"]'},
                                    timeout=65)
                backoff = 1.0
            except HopperError as exc:
                if "409" in str(exc):
                    print("telegram: another program is polling this bot; "
                          "only one may (stop it, or use a separate bot)", file=sys.stderr)
                else:
                    print(f"telegram: {exc}", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates or []:
                offset = u["update_id"] + 1
                msg = self._to_message(u)
                if msg:
                    on_message(msg)
            if updates:
                os.makedirs(os.path.dirname(state), exist_ok=True)
                with open(state, "w") as fh:
                    fh.write(str(offset))
