"""`hop relay`: chat with Claude Code from Telegram or WhatsApp.

The relay is a small always-on process with no AI in it. It listens on the configured
channels and, per chat, keeps a Claude Code process warm while you're talking:

  - the first message of a conversation starts `claude -p` in streaming mode (a few
    seconds); follow-ups reuse the warm process and answer in about a second
  - after `warm_minutes` without a message the process exits; the next message resumes
    the same Claude conversation (--resume), so nothing is open between chats
  - replies stream into the chat (edited in place where the app allows)
  - permission prompts come to the chat as "yes abcde / no abcde"; nothing is approved
    without you, and an unanswered prompt is denied after `approval_minutes`
  - voice notes and videos are turned into text first (Hopper's media job types);
    photos and files are handed to Claude by path
  - `instant` rules run a command for simple phrases (e.g. "lights red") with no AI
  - if Claude can't answer (usage limit, login expired, crash) the message goes to the
    `fallback` model instead, marked as such

Chat commands: /new (fresh conversation), /opus /sonnet /haiku (switch model, same
conversation), /stop (interrupt the current reply), /status.

Personal use: the relay runs Claude Code under your own login on your own machine, for
you. Don't use it to offer Claude to other people; that breaks Anthropic's terms.

    [relay]
    channels = ["telegram"]
    cwd = "~"                          # where Claude works
    model = "sonnet"
    warm_minutes = 10
    approval_minutes = 10
    allowed_tools = ["Read", "Grep", "Glob", "Bash(git status:*)"]
    append_system_prompt_file = "~/.config/hopper/relay-prompt.md"
    mcp_config = "~/.config/hopper/relay-mcp.json"   # e.g. Hopper's own MCP, to queue jobs

    [[relay.instant]]
    match = '^(?i)(lights?|leds?)\\b.*'
    run = ["~/bin/lights", "{text}"]

    [relay.fallback]
    url = "http://127.0.0.1:8642/v1/chat/completions"   # any OpenAI-compatible endpoint
    key = "env:FALLBACK_KEY"
    model = "anthropic/claude-sonnet-5.5"
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import channels as chans
from .config import Config, resolve_secret
from .media import Media
from .util import HopperError, expand

MODELS = {"/opus": "opus", "/sonnet": "sonnet", "/haiku": "haiku"}
APPROVAL_RE = re.compile(r"^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$", re.I)
CLAUDE_DOWN = re.compile(r"usage limit|rate.?limit|overloaded|login|authenticat|credit|quota", re.I)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} relay: {msg}", file=sys.stderr, flush=True)


class ClaudeDown(Exception):
    """Claude Code can't answer right now; use the fallback."""


class Agent:
    """One warm `claude -p` process in streaming mode, for one chat."""

    def __init__(self, relay: "Relay", address: str):
        self.relay, self.address = relay, address
        self.proc: subprocess.Popen | None = None
        self.model = ""
        self.events: queue.Queue = queue.Queue()
        self.last_used = 0.0
        self.busy = False

    @property
    def session_id(self) -> str:
        return self.relay.sessions.get(self.address, {}).get("session_id", "")

    def _argv(self, model: str) -> list[str]:
        r = self.relay.conf
        argv = [r.get("claude", "claude"), "-p", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--model", model, "--permission-prompt-tool", "stdio"]
        if self.session_id:
            argv += ["--resume", self.session_id]
        tools = r.get("allowed_tools") or []
        if tools:
            argv += ["--allowedTools", ",".join(tools)]
        prompt_file = r.get("append_system_prompt_file")
        if prompt_file and os.path.exists(expand(prompt_file)):
            argv += ["--append-system-prompt", open(expand(prompt_file)).read()]
        mcp = r.get("mcp_config")
        if mcp:
            argv += ["--strict-mcp-config", "--mcp-config", expand(mcp)]
        return argv

    def start(self, model: str) -> None:
        self.stop()
        self.events = queue.Queue()
        argv = self._argv(model)
        if not shutil.which(argv[0]):
            raise ClaudeDown(f"{argv[0]} is not installed")
        self.proc = subprocess.Popen(argv, cwd=expand(self.relay.conf.get("cwd", "~")),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.model = model
        proc, events = self.proc, self.events

        def pump():
            for line in proc.stdout:
                try:
                    events.put(json.loads(line))
                except ValueError:
                    pass
            events.put({"type": "_exit", "code": proc.wait()})
        threading.Thread(target=pump, daemon=True).start()
        log(f"{self.address}: started claude ({model}{', resuming' if self.session_id else ''})")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        if self.proc:
            for pipe in (self.proc.stdout, self.proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        self.proc = None

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _write(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def ask(self, text: str, model: str, on_text, on_permission) -> str:
        """Send one message; stream text via on_text(full_so_far); return the final text."""
        if not self.alive() or model != self.model:
            self.start(model)
        self.busy, self.last_used = True, time.time()
        try:
            self._write({"type": "user", "message": {"role": "user", "content": text}})
            order, final, partial, current = [], {}, {}, None
            stderr_hint = ""
            while True:
                try:
                    ev = self.events.get(timeout=1800)
                except queue.Empty:
                    raise ClaudeDown("no answer from claude for 30 minutes") from None
                typ = ev.get("type")
                if typ == "_exit":
                    try:
                        stderr_hint = self.proc.stderr.read()[-500:] if self.proc else ""
                    except (OSError, ValueError):
                        pass
                    self.proc = None
                    raise ClaudeDown(f"claude exited ({ev['code']}): {stderr_hint.strip()}")
                if typ == "system" and ev.get("session_id"):
                    self.relay.remember(self.address, ev["session_id"], self.model)
                elif typ == "stream_event":
                    e = ev.get("event") or {}
                    if e.get("type") == "message_start":
                        current = (e.get("message") or {}).get("id") or f"m{len(order)}"
                        order.append(current)
                    elif e.get("type") == "content_block_delta" and current:
                        d = e.get("delta") or {}
                        if d.get("type") == "text_delta":
                            partial[current] = partial.get(current, "") + d.get("text", "")
                            on_text(_join(order, final, partial))
                elif typ == "assistant":
                    msg = ev.get("message") or {}
                    mid = msg.get("id") or current or f"m{len(order)}"
                    if mid not in order:
                        order.append(mid)
                    final[mid] = "".join(b.get("text", "") for b in msg.get("content") or []
                                         if b.get("type") == "text")
                    on_text(_join(order, final, partial))
                elif typ == "control_request":
                    req = ev.get("request") or {}
                    if req.get("subtype") == "can_use_tool":
                        ok, why = on_permission(req.get("tool_name", "?"), req.get("input") or {})
                        resp = ({"behavior": "allow", "updatedInput": req.get("input") or {}} if ok
                                else {"behavior": "deny", "message": why})
                    else:
                        resp = {}
                    self._write({"type": "control_response", "response": {
                        "subtype": "success", "request_id": ev.get("request_id"), "response": resp}})
                elif typ == "result":
                    if ev.get("session_id"):
                        self.relay.remember(self.address, ev["session_id"], self.model)
                    text = _join(order, final, partial) or ev.get("result") or ""
                    if ev.get("is_error") and CLAUDE_DOWN.search(str(ev.get("result", ""))):
                        raise ClaudeDown(str(ev.get("result"))[:300])
                    return text.strip()
        finally:
            self.busy, self.last_used = False, time.time()

    def interrupt(self) -> None:
        if self.alive():
            try:
                self._write({"type": "control_request", "request_id": secrets.token_hex(6),
                             "request": {"subtype": "interrupt"}})
            except OSError:
                pass


def _join(order, final, partial) -> str:
    return "\n\n".join(t for t in (final.get(m) or partial.get(m, "") for m in order) if t.strip())


class Relay:
    def __init__(self, cfg: Config):
        self.cfg, self.conf = cfg, cfg.relay
        if not self.conf:
            raise HopperError("no [relay] section in the config")
        names = self.conf.get("channels") or list(cfg.channels)
        if not names:
            raise HopperError("[relay] channels is empty and no [channels.*] are configured")
        self.channels = {n: chans.get(cfg, n) for n in names}
        self.media = Media(cfg)
        self.state = expand(self.conf.get("state_file") or "~/.local/share/hopper/relay-sessions.json")
        try:
            self.sessions = json.load(open(self.state))
        except (OSError, ValueError):
            self.sessions = {}
        self.agents: dict[str, Agent] = {}
        self.inboxes: dict[str, queue.Queue] = {}
        self.approvals: dict[str, dict] = {}      # code -> {"address", "event", "ok"}
        self.lock = threading.Lock()
        self.warm = float(self.conf.get("warm_minutes", 10)) * 60
        self.instant = [(re.compile(r["match"]), [expand(r["run"][0])] + list(r["run"][1:]))
                        for r in self.conf.get("instant") or []]

    # -- state ----------------------------------------------------------------
    def remember(self, address: str, session_id: str, model: str) -> None:
        with self.lock:
            cur = self.sessions.get(address, {})
            if cur.get("session_id") == session_id and cur.get("model") == model:
                return
            self.sessions[address] = {"session_id": session_id, "model": model, "at": time.time()}
            os.makedirs(os.path.dirname(self.state), exist_ok=True)
            with open(self.state, "w") as fh:
                json.dump(self.sessions, fh, indent=1)

    def model_for(self, address: str) -> str:
        return self.sessions.get(address, {}).get("model") or self.conf.get("model", "sonnet")

    # -- inbound --------------------------------------------------------------
    def on_message(self, msg: chans.Message) -> None:
        """Called from a channel's receive thread. Fast paths here; the rest is queued."""
        m = APPROVAL_RE.match(msg.text or "")
        if m and m.group(2).lower() in self.approvals:
            a = self.approvals[m.group(2).lower()]
            if a["address"] == msg.address:
                a["ok"] = m.group(1).lower().startswith("y")
                a["event"].set()
                return
        text = (msg.text or "").strip()
        ch = self.channels[msg.channel]
        if text == "/stop":
            agent = self.agents.get(msg.address)
            if agent and agent.busy:
                agent.interrupt()
                ch.send(msg.chat, "Stopping.")
            else:
                ch.send(msg.chat, "Nothing is running.")
            return
        if text == "/status":
            agent = self.agents.get(msg.address)
            sess = self.sessions.get(msg.address, {})
            ch.send(msg.chat, f"model {self.model_for(msg.address)} · "
                              f"{'warm' if agent and agent.alive() else 'cold'}"
                              f"{' · busy' if agent and agent.busy else ''} · "
                              f"conversation {sess.get('session_id', 'new')[:8]}")
            return
        for rx, argv in self.instant:
            if text and not msg.files and rx.match(text):
                threading.Thread(target=self._instant, args=(msg, rx, argv), daemon=True).start()
                return
        with self.lock:
            box = self.inboxes.get(msg.address)
            if box is None:
                box = self.inboxes[msg.address] = queue.Queue()
                threading.Thread(target=self._chat_loop, args=(msg.address, box), daemon=True).start()
        box.put(msg)

    def _instant(self, msg, rx, argv) -> None:
        ch = self.channels[msg.channel]
        groups = rx.match(msg.text.strip()).groups()
        args = [a.replace("{text}", msg.text.strip()) for a in argv]
        for i, g in enumerate(groups, 1):
            args = [a.replace("{%d}" % i, g or "") for a in args]
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=60)
            out = (p.stdout.strip() or "Done.") if p.returncode == 0 else f"That failed: {p.stderr.strip()[:300]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            out = f"That failed: {exc}"
        ch.send(msg.chat, out)

    # -- one chat -------------------------------------------------------------
    def _chat_loop(self, address: str, box: queue.Queue) -> None:
        while True:
            msg = box.get()
            try:
                self._handle(msg)
            except Exception as exc:   # one bad message must not kill the chat
                log(f"{address}: {type(exc).__name__}: {exc}")
                try:
                    self.channels[msg.channel].send(msg.chat, f"Something broke: {exc}")
                except Exception:
                    pass

    def _prepare(self, msg: chans.Message) -> str:
        """The text Claude sees: voice and video become text, other files are passed by path."""
        parts = [msg.text.strip()] if msg.text.strip() else []
        for path in msg.files:
            kind = chans.file_kind(path)
            try:
                if kind == "voice":
                    heard = self.media.transcribe(path)["text"]
                    parts.insert(0, heard if not parts else f"(voice note) {heard}")
                elif kind == "video":
                    seen = self.media.describe(path, msg.text or "")["text"]
                    parts.append(f"[He sent a video, saved at {path}. What it shows: {seen}]")
                elif kind == "image":
                    parts.append(f"[He sent an image: {path} — read it to see it]")
                else:
                    parts.append(f"[He sent a file: {path}]")
            except HopperError as exc:
                parts.append(f"[He sent {os.path.basename(path)} but it couldn't be processed: {exc}]")
        return "\n".join(parts)

    def _handle(self, msg: chans.Message) -> None:
        ch = self.channels[msg.channel]
        text = msg.text.strip()
        if text == "/new":
            agent = self.agents.pop(msg.address, None)
            if agent:
                agent.stop()
            with self.lock:
                self.sessions.pop(msg.address, None)
            ch.send(msg.chat, "Fresh conversation.")
            return
        if text.split(" ", 1)[0] in MODELS:
            cmd, _, rest = text.partition(" ")
            model = MODELS[cmd]
            sid = self.sessions.get(msg.address, {}).get("session_id", "")
            if sid:
                self.remember(msg.address, sid, model)
            else:
                with self.lock:
                    self.sessions[msg.address] = {"model": model}
            if not rest.strip() and not msg.files:
                ch.send(msg.chat, f"Using {model}.")
                return
            msg.text = rest
        ch.typing(msg.chat)
        prompt = self._prepare(msg)
        if not prompt:
            return
        agent = self.agents.get(msg.address) or self.agents.setdefault(msg.address, Agent(self, msg.address))

        sent = {"id": "", "shown": "", "at": 0.0}
        stop_typing = threading.Event()

        def keep_typing():
            while not stop_typing.wait(4):
                ch.typing(msg.chat)
        threading.Thread(target=keep_typing, daemon=True).start()

        def on_text(full: str) -> None:
            if not ch.can_edit or not full.strip() or time.time() - sent["at"] < 1.5:
                return
            view = full if len(full) < 3900 else full[:3900] + " …"
            if not sent["id"]:
                sent["id"] = ch.send(msg.chat, view)
            elif view != sent["shown"]:
                ch.edit(msg.chat, sent["id"], view)
            sent["shown"], sent["at"] = view, time.time()

        def on_permission(tool: str, inp: dict):
            return self._approve(ch, msg, tool, inp)

        try:
            reply = agent.ask(prompt, self.model_for(msg.address), on_text, on_permission)
        except ClaudeDown as exc:
            log(f"{msg.address}: claude unavailable: {exc}")
            reply = self._fallback(prompt, msg.address, str(exc))
        finally:
            stop_typing.set()
        reply = reply or "(no reply)"
        if sent["id"] and len(reply) <= 3900:
            if reply != sent["shown"]:
                ch.edit(msg.chat, sent["id"], reply)
        elif sent["id"]:
            ch.edit(msg.chat, sent["id"], reply[:3900])
            ch.send(msg.chat, reply[3900:])
        else:
            ch.send(msg.chat, reply)

    def _approve(self, ch, msg, tool: str, inp: dict):
        code = "".join(secrets.choice("abcdefghijkmnopqrstuvwxyz") for _ in range(5))
        event = threading.Event()
        self.approvals[code] = {"address": msg.address, "event": event, "ok": False}
        detail = inp.get("command") or inp.get("file_path") or inp.get("url") or json.dumps(inp)[:300]
        ch.send(msg.chat, f"Claude wants to use {tool}:\n{str(detail)[:600]}\n\n"
                          f"Reply  yes {code}  or  no {code}")
        minutes = float(self.conf.get("approval_minutes", 10))
        answered = event.wait(minutes * 60)
        a = self.approvals.pop(code)
        if not answered:
            ch.send(msg.chat, f"No answer in {minutes:.0f} min, so that's a no.")
            return False, "The user did not approve this in time."
        return (True, "") if a["ok"] else (False, "The user said no.")

    def _fallback(self, prompt: str, address: str, why: str) -> str:
        fb = self.conf.get("fallback") or {}
        if not fb.get("url"):
            return f"Claude can't answer right now ({why[:200]}) and no fallback is configured."
        headers = {"Content-Type": "application/json", **(fb.get("headers") or {})}
        if fb.get("key"):
            headers["Authorization"] = f"Bearer {resolve_secret(fb['key'])}"
        body = {"model": fb.get("model", ""), "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(fb["url"], data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=float(fb.get("timeout", 180))) as r:
                out = json.load(r)["choices"][0]["message"]["content"]
            return f"{out}\n\n(fallback: Claude unavailable)"
        except Exception as exc:
            return f"Claude can't answer ({why[:150]}) and the fallback failed too: {exc}"

    # -- run ------------------------------------------------------------------
    def _reaper(self) -> None:
        while True:
            time.sleep(30)
            for address, agent in list(self.agents.items()):
                if agent.alive() and not agent.busy and time.time() - agent.last_used > self.warm:
                    log(f"{address}: idle {self.warm / 60:.0f} min, letting claude exit")
                    agent.stop()

    def run(self) -> int:
        threading.Thread(target=self._reaper, daemon=True).start()
        threads = []
        for name, ch in self.channels.items():
            t = threading.Thread(target=ch.receive, args=(self.on_message,), name=name, daemon=True)
            t.start()
            threads.append(t)
            log(f"listening on {name}")
        try:
            while all(t.is_alive() for t in threads):
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        for agent in self.agents.values():
            agent.stop()
        log("a channel stopped; exiting so systemd restarts the relay")
        return 1
