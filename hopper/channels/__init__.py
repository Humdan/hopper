"""Channels: messaging apps Hopper can listen on and talk through.

A channel turns an app (Telegram, WhatsApp, ...) into the same small interface, so
`hop relay` and `hop send` never care which app a message came from:

    receive(on_message)    block, calling on_message(Message) for each allowed message
    send(chat, text)       -> message id ('' when the app can't edit later)
    send_file(chat, path, caption)  an image, voice note, video or document, by file type
    edit(chat, id, text)   replace an earlier message (no-op where unsupported)
    typing(chat)           show "typing..." if the app has it

Every channel has an allowlist. Messages from anyone else are dropped before they reach
an agent, and `hop send` refuses chats that aren't listed.

Configured in config.toml:

    [channels.telegram]
    token = "env:HOPPER_TELEGRAM_TOKEN"
    allow = ["123456789"]

    [channels.whatsapp]
    token = "env:HOPPER_WHATSAPP_TOKEN"
    phone_number_id = "1234567890"
    app_secret = "env:HOPPER_WHATSAPP_APP_SECRET"
    verify_token = "env:HOPPER_WHATSAPP_VERIFY"
    allow = ["15551234567"]
"""
from __future__ import annotations

import mimetypes
import os
import secrets
from dataclasses import dataclass, field

from ..config import Config, resolve_secret
from ..util import HopperError


@dataclass
class Message:
    channel: str          # "telegram", "whatsapp"
    chat: str             # where to reply
    sender: str           # who sent it (same as chat in a DM)
    text: str
    id: str = ""          # the app's message id
    files: list[str] = field(default_factory=list)   # local paths of attachments

    @property
    def address(self) -> str:
        return f"{self.channel}:{self.chat}"


class Channel:
    name = ""
    can_edit = False

    def __init__(self, conf: dict):
        self.conf = conf
        self.allow = {str(a) for a in (conf.get("allow") or ())}
        if not self.allow:
            raise HopperError(f"[channels.{self.name}] needs an allow list; nobody would be let in")

    def allowed(self, chat: str) -> bool:
        return str(chat) in self.allow

    def check(self, chat: str) -> None:
        if not self.allowed(chat):
            raise HopperError(f"{self.name} chat {chat} is not in [channels.{self.name}] allow")

    def secret(self, key: str, required: bool = True) -> str:
        value = self.conf.get(key)
        if value is None:
            if required:
                raise HopperError(f"[channels.{self.name}] needs {key}")
            return ""
        return resolve_secret(value)

    # The interface. Subclasses implement these.
    def receive(self, on_message) -> None:
        raise NotImplementedError

    def send(self, chat: str, text: str) -> str:
        raise NotImplementedError

    def send_file(self, chat: str, path: str, caption: str = "") -> str:
        raise NotImplementedError

    def edit(self, chat: str, message_id: str, text: str) -> None:
        pass

    def typing(self, chat: str) -> None:
        pass


def file_kind(path: str) -> str:
    """'image', 'voice', 'video' or 'document', from the file name."""
    mime = mimetypes.guess_type(path)[0] or ""
    if path.lower().endswith((".ogg", ".oga", ".opus")):
        return "voice"
    if mime.startswith("image/") and not path.lower().endswith(".svg"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "voice"
    return "document"


def multipart(fields: dict, file_field: str, path: str) -> tuple[bytes, str]:
    """A multipart/form-data body with one file (stdlib has no helper for this)."""
    boundary = "hopper" + secrets.token_hex(12)
    out = []
    for k, v in fields.items():
        if v is None:
            continue
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    name = os.path.basename(path).replace('"', "")
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{name}"\r\n'
               f"Content-Type: {mime}\r\n\r\n".encode())
    with open(path, "rb") as fh:
        out.append(fh.read())
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def get(cfg: Config, name: str) -> Channel:
    conf = cfg.channels.get(name)
    if conf is None:
        raise HopperError(f"no [channels.{name}] in the config")
    if name == "telegram":
        from .telegram import Telegram
        return Telegram(conf)
    if name == "whatsapp":
        from .whatsapp import WhatsApp
        return WhatsApp(conf)
    raise HopperError(f"unknown channel {name!r}; known: telegram, whatsapp")


def send(cfg: Config, address: str, text: str, files=()) -> str:
    """Send text (and files) to 'channel:chat'. Used by `hop send` and the reply_to sinks."""
    name, _, chat = address.partition(":")
    if not chat:
        raise HopperError(f"address must be channel:chat (got {address!r})")
    ch = get(cfg, name)
    ch.check(chat)
    last = ch.send(chat, text) if (text or "").strip() else ""
    for path in files or ():
        if not os.path.isfile(path):
            raise HopperError(f"no such file to send: {path}")
        last = ch.send_file(chat, path)
    return last
