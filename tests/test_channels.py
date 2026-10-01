"""Channels, media job types and the relay, with no network and no real Claude."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from hopper import channels as chans
from hopper.channels.telegram import Telegram, chunks
from hopper.config import Config
from hopper.core import Hopper
from hopper.media import Media, job_input
from hopper.relay import Relay
from hopper.util import HopperError


class FakeChannel(chans.Channel):
    name = "fake"
    can_edit = True

    def __init__(self, conf=None):
        super().__init__(conf or {"allow": ["42"]})
        self.sent, self.edits = [], []

    def send(self, chat, text):
        self.sent.append((chat, text))
        return str(len(self.sent))

    def send_file(self, chat, path, caption=""):
        self.sent.append((chat, f"<file {os.path.basename(path)}>"))
        return str(len(self.sent))

    def edit(self, chat, message_id, text):
        self.edits.append((message_id, text))


# A stand-in for `claude -p --input-format stream-json ...`: streams a reply, asks
# permission when the prompt says "tool", and fails like a usage limit on "limit".
FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, sys
    def out(o): print(json.dumps(o), flush=True)
    out({"type": "system", "subtype": "init", "session_id": "sess-1"})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") != "user":
            continue
        text = msg["message"]["content"]
        if "limit" in text:
            out({"type": "result", "is_error": True, "result": "Claude usage limit reached", "session_id": "sess-1"})
            continue
        if "tool" in text:
            out({"type": "control_request", "request_id": "r1",
                 "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm x"}}})
            resp = json.loads(sys.stdin.readline())
            verdict = resp["response"]["response"]["behavior"]
            reply = "ran it" if verdict == "allow" else "not allowed"
        else:
            reply = "echo: " + text
        out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}})
        out({"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta", "text": reply}}})
        out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": reply}]}})
        out({"type": "result", "is_error": False, "result": reply, "session_id": "sess-1"})
''')


class ChannelTests(unittest.TestCase):
    def test_allowlist_required(self):
        with self.assertRaises(HopperError):
            FakeChannel({"allow": []})

    def test_send_refuses_unlisted_chat(self):
        cfg = Config(channels={"telegram": {"token": "t", "allow": ["1"]}})
        with self.assertRaises(HopperError):
            chans.send(cfg, "telegram:2", "hi")

    def test_chunks_split_long_text_on_lines(self):
        parts = chunks("a" * 3000 + "\n" + "b" * 3000)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(len(p) <= 4000 for p in parts))

    def test_file_kind(self):
        self.assertEqual(chans.file_kind("x.ogg"), "voice")
        self.assertEqual(chans.file_kind("x.png"), "image")
        self.assertEqual(chans.file_kind("x.mp4"), "video")
        self.assertEqual(chans.file_kind("x.pdf"), "document")

    def test_multipart_body(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as fh:
            fh.write(b"PNGDATA")
            fh.flush()
            body, ctype = chans.multipart({"chat_id": "1"}, "photo", fh.name)
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b"PNGDATA", body)
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))

    def test_telegram_drops_strangers(self):
        tg = Telegram({"token": "t", "allow": ["7"]})
        self.assertIsNone(tg._to_message({"message": {"chat": {"id": 9}, "from": {"id": 9}, "text": "hi"}}))
        msg = tg._to_message({"message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "hi", "message_id": 3}})
        self.assertEqual((msg.address, msg.text), ("telegram:7", "hi"))


class MediaTests(unittest.TestCase):
    def test_job_input_shapes(self):
        self.assertEqual(job_input("image", "a fox", "", []), {"prompt": "a fox", "files": []})
        self.assertEqual(job_input("speak", "hello", "", []), {"text": "hello"})
        with self.assertRaises(HopperError):
            job_input("transcribe", "x", "", [])

    def test_inputs_outside_allowed_dirs_are_refused(self):
        m = Media(Config(media={"input_dirs": [tempfile.gettempdir() + "/nothing-here"]}))
        with self.assertRaises(HopperError):
            m._need("/etc/hostname")

    def test_typed_job_requires_capability(self):
        with tempfile.TemporaryDirectory() as d:
            hop = Hopper(os.path.join(d, "h.db"), Config())
            job = hop.add("a fox", type="image", input={"prompt": "a fox"})
            self.assertIn("media.image", job["requires"])
            self.assertEqual(job["meta"]["type"], "image")
            self.assertIsNone(hop.claim(worker="w", caps=["shell"]))
            self.assertEqual(hop.claim(worker="w", caps=["media.image"])["id"], job["id"])
            with self.assertRaises(HopperError):
                hop.add("x", type="hologram")

    def test_done_notice_carries_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            hop = Hopper(os.path.join(d, "h.db"), Config(file_dir=d))
            job = hop.add("img", type="image", input={"prompt": "x"}, reply_to="file:out.jsonl")
            hop.claim(worker="w", caps=["media.image"])
            hop.complete(job["id"], "w", "here", artifacts=["/tmp/a.png"])
            hop.flush()
            notice = json.loads(open(os.path.join(d, "out.jsonl")).read().splitlines()[-1])
            self.assertEqual(notice["artifacts"], ["/tmp/a.png"])


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude")
        with open(self.claude, "w") as fh:
            fh.write(FAKE_CLAUDE.replace("/usr/bin/env python3", sys.executable))
        os.chmod(self.claude, os.stat(self.claude).st_mode | stat.S_IEXEC)
        lights = os.path.join(self.tmp.name, "lights")
        with open(lights, "w") as fh:
            fh.write("#!/bin/sh\necho \"set $1\"\n")
        os.chmod(lights, 0o755)
        cfg = Config(channels={"fake": {"allow": ["42"]}}, relay={
            "channels": ["fake"], "claude": self.claude, "cwd": self.tmp.name,
            "state_file": os.path.join(self.tmp.name, "sessions.json"), "approval_minutes": 0.05,
            "instant": [{"match": r"^lights (\w+)$", "run": [lights, "{1}"]}]})
        self.fake = FakeChannel()
        with mock.patch.object(chans, "get", lambda cfg, name: self.fake):
            self.relay = Relay(cfg)

    def tearDown(self):
        for a in self.relay.agents.values():
            a.stop()
        self.tmp.cleanup()

    def say(self, text):
        self.relay.on_message(chans.Message("fake", "42", "42", text))

    def wait_for(self, pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        self.fail(f"timed out; sent={self.fake.sent} edits={self.fake.edits}")

    def replies(self):
        return [t for _, t in self.fake.sent] + [t for _, t in self.fake.edits]

    def test_reply_streams_and_session_is_remembered(self):
        self.say("hello")
        self.wait_for(lambda: any("echo: hello" in t for t in self.replies()))
        self.wait_for(lambda: self.relay.sessions.get("fake:42", {}).get("session_id") == "sess-1")

    def test_warm_process_is_reused(self):
        self.say("one")
        self.wait_for(lambda: any("echo: one" in t for t in self.replies()))
        pid = self.relay.agents["fake:42"].proc.pid
        self.say("two")
        self.wait_for(lambda: any("echo: two" in t for t in self.replies()))
        self.assertEqual(self.relay.agents["fake:42"].proc.pid, pid)

    def test_permission_goes_to_chat_and_yes_allows(self):
        self.say("use a tool")
        self.wait_for(lambda: any("Reply  yes" in t for t in self.replies()))
        code = [t for t in self.replies() if "Reply  yes" in t][0].split()[-1]
        self.say(f"yes {code}")
        self.wait_for(lambda: any("ran it" in t for t in self.replies()))

    def test_unanswered_permission_is_denied(self):
        self.say("use a tool")
        self.wait_for(lambda: any("not allowed" in t for t in self.replies()))

    def test_usage_limit_uses_fallback_message(self):
        self.say("hit the limit")
        self.wait_for(lambda: any("no fallback is configured" in t for t in self.replies()))

    def test_instant_rule_skips_claude(self):
        self.say("lights red")
        self.wait_for(lambda: ("42", "set red") in self.fake.sent)
        self.assertNotIn("fake:42", self.relay.agents)

    def test_new_forgets_the_conversation(self):
        self.say("hello")
        self.wait_for(lambda: "fake:42" in self.relay.sessions)
        self.say("/new")
        self.wait_for(lambda: ("42", "Fresh conversation.") in self.fake.sent)
        self.assertNotIn("fake:42", self.relay.sessions)


if __name__ == "__main__":
    unittest.main()
