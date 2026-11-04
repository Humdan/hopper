"""Failures nobody is waiting on get logged and alerted. No test here reaches a real notifier."""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from hopper import Hopper, util
from hopper.channels.telegram import Telegram
from hopper.config import Config, Token
from hopper.core import _OUTBOX_GIVE_UP
from hopper.mcp import run as mcp_run
from hopper.worker import Runner
from tests import REAL_RUN_NOTIFIER
from tests.test_interfaces import free_port


def make(tmp, **kw):
    return Hopper(config=Config(db=os.path.join(tmp, "h.db"), file_dir=os.path.join(tmp, "n"), **kw))


class Undeliverable(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = make(self._tmp.name, webhook_allow=("http://127.0.0.1:9/",))
        self.addCleanup(self.h.close)
        patcher = mock.patch("hopper.core.alert")
        self.alert = patcher.start()
        self.addCleanup(patcher.stop)

    def _fail_notice(self):
        j = self.h.add("x", reply_to="webhook:http://127.0.0.1:9/secret-path")
        self.h.claim("w")
        self.h.complete(j["id"], "w", "done")
        return j

    def _flush_due(self):
        self.h._db.execute("UPDATE outbox SET next_at = 0")
        return self.h.flush()

    def test_alerts_once_on_giving_up_and_not_before(self):
        j = self._fail_notice()
        for _ in range(_OUTBOX_GIVE_UP - 1):
            self._flush_due()
        self.alert.assert_not_called()
        self._flush_due()
        self.alert.assert_called_once()
        key, text = self.alert.call_args.args
        self.assertEqual(key, "hopper:undeliverable:webhook")
        self.assertIn(j["id"], text)
        self.assertNotIn("secret-path", key + text)
        self.assertEqual(self._flush_due(), {"sent": 0, "failed": 0})
        self.alert.assert_called_once()
        stats = self.h.stats()
        self.assertEqual((stats["outbox_pending"], stats["outbox_stuck"]), (0, 1))

    def test_many_at_once_is_one_alert(self):
        for _ in range(3):
            self._fail_notice()
        for _ in range(_OUTBOX_GIVE_UP):
            self._flush_due()
        self.alert.assert_called_once()
        self.assertIn("3 webhook notice(s)", self.alert.call_args.args[1])

    def test_cause_seen_on_this_box_missing_token(self):
        env = {k: v for k, v in os.environ.items() if k != "HOPPER_TELEGRAM_TOKEN"}
        os.makedirs(self._tmp.name + "/t")
        h = make(self._tmp.name + "/t",
                 channels={"telegram": {"token": "env:HOPPER_TELEGRAM_TOKEN", "allow": ["1"]}})
        self.addCleanup(h.close)
        j = h.add("x", reply_to="telegram:1")
        h.claim("w")
        h.complete(j["id"], "w", "done")
        with mock.patch.dict(os.environ, env, clear=True):
            for _ in range(_OUTBOX_GIVE_UP):
                h._db.execute("UPDATE outbox SET next_at = 0")
                h.flush()
        self.assertIn("HOPPER_TELEGRAM_TOKEN, which is not set", self.alert.call_args.args[1])


class Alert(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.err = io.StringIO()
        for p in (mock.patch("sys.stderr", self.err),
                  mock.patch.dict(os.environ, {"HOME": self._tmp.name, "PATH": self._tmp.name}),
                  mock.patch("sys.argv", [os.path.join(self._tmp.name, "hop")]),
                  mock.patch.object(util, "_run_notifier", REAL_RUN_NOTIFIER)):  # HOME and PATH are fakes here
            p.start()
            self.addCleanup(p.stop)

    def _join(self):
        for t in threading.enumerate():
            if t is not threading.current_thread() and not t.daemon:
                t.join(10)

    def _fake(self, name, rc=0):
        path = os.path.join(self._tmp.name, name)
        log = path + ".log"
        with open(path, "w") as fh:
            fh.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\nexit {rc}\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return log

    def test_absent_notifier_is_only_a_log_line(self):
        with mock.patch.object(util.subprocess, "run") as run:
            util.alert("k", "broken")
            self._join()
        run.assert_not_called()
        self.assertIn("hopper: broken", self.err.getvalue())

    def test_calls_the_notifier_with_a_dedupe_key(self):
        log = self._fake("hopper-notify")
        util.alert("k", "broken")
        self._join()
        with open(log) as fh:
            self.assertEqual(fh.read().split("\n")[:4], ["-l", "warn", "--key", "k"])

    def test_found_outside_path_beside_hop(self):
        log = self._fake("hopper-notify")
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self._tmp.name, "empty")}):
            util.alert("k", "broken")
            self._join()
        self.assertTrue(os.path.exists(log))

    def test_failing_notifier_only_logs(self):
        self._fake("hopper-notify", rc=1)
        util.alert("k", "broken")
        self._join()
        self.assertIn("exited 1", self.err.getvalue())
        with mock.patch.object(util.subprocess, "run", side_effect=OSError("nope")):
            REAL_RUN_NOTIFIER("x", "k", "broken")
        self.assertIn("nope", self.err.getvalue())


class ServerBadRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.hop = make(cls._tmp.name, tokens=[Token("t", "t" * 20, ("admin",))])
        cls.port = free_port()
        from hopper.server import serve
        cls.alerts = mock.patch("hopper.server.alert").start()
        threading.Thread(target=serve, args=(cls.hop,), daemon=True,
                         kwargs={"port": cls.port, "quiet": True, "housekeeping_s": 0.01}).start()
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        mock.patch.stopall()   # the server thread keeps running; its database goes with the process
        cls._tmp.cleanup()

    def _req(self, path, data=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Authorization": "Bearer " + "t" * 20, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_bad_limit_is_400(self):
        self.assertEqual(self._req("/v1/jobs?limit=abc")[0], 400)

    def test_bad_content_length_is_400(self):
        self.assertEqual(self._req("/v1/stats", data=b"", headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self._req("/v1/stats", data=b"", headers={"Content-Length": "abc"})[0], 400)

    def test_value_error_is_400_and_unexpected_error_is_500(self):
        with mock.patch.object(self.hop, "stats", side_effect=ValueError("no")):
            self.assertEqual(self._req("/v1/stats", data=b"{}")[0], 400)
        with mock.patch.object(self.hop, "stats", side_effect=RuntimeError("boom")), \
                mock.patch("hopper.server.traceback.print_exc") as tb, mock.patch("builtins.print"):
            code, body = self._req("/v1/stats", data=b"{}")
        self.assertEqual(code, 500)
        self.assertNotIn("boom", body["error"])
        tb.assert_called_once()
        self.assertEqual(self._req("/v1/stats", data=b"{}")[0], 200)

    def test_housekeeping_alerts_once_per_outage(self):
        self.alerts.reset_mock()
        with mock.patch.object(self.hop, "flush", side_effect=RuntimeError("disk")), mock.patch("builtins.print"):
            for _ in range(200):
                time.sleep(0.01)
                if self.alerts.called:
                    break
            time.sleep(0.2)
        self.alerts.assert_called_once()
        self.assertEqual(self.alerts.call_args.args[0], "hopper:housekeeping")
        self.assertIn("disk", self.alerts.call_args.args[1])


class Mcp(unittest.TestCase):
    def test_bad_input_and_unexpected_errors_do_not_end_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = make(tmp)
            lines = ["[1]", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]}),
                     json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "hop_stats", "arguments": {}}}),
                     json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"})]
            out = io.StringIO()
            with mock.patch("hopper.mcp.connect", return_value=h), \
                    mock.patch.object(h, "stats", side_effect=RuntimeError("boom")), \
                    mock.patch("hopper.mcp.traceback.print_exc"):
                mcp_run(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
            h.close()
        replies = [json.loads(ln) for ln in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertEqual(replies[1]["error"]["code"], -32602)
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertIn("boom", replies[2]["result"]["content"][0]["text"])
        self.assertEqual(replies[3]["result"], {})


class TelegramErrors(unittest.TestCase):
    def setUp(self):
        self.t = Telegram({"token": "SECRET-TOKEN", "allow": ["1"]})

    def test_read_timeout_is_a_clean_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(util.HopperError) as cm:
                self.t.send("1", "hi")
        self.assertIn("unreachable", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_non_json_answer_is_a_clean_error(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b"<html>")
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(util.HopperError) as cm:
                self.t.send("1", "hi")
        self.assertIn("isn't JSON", str(cm.exception))


class Worker(unittest.TestCase):
    def test_flush_failure_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = make(tmp)
            r = Runner(h, sys.executable, name="t", poll=0.01, log_dir=tmp)

            def fail():
                r.stopping.set()
                raise util.HopperError("no way")

            err = io.StringIO()
            with mock.patch.object(h, "flush", side_effect=fail), mock.patch("sys.stderr", err), \
                    mock.patch("hopper.worker.signal.signal"), mock.patch("builtins.print", wraps=print):
                r.run()
            h.close()
        self.assertIn("flush failed: no way", err.getvalue())


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cfg = os.path.join(self._tmp.name, "c.toml")
        with open(cfg, "w") as fh:
            fh.write('[channels.telegram]\ntoken = "env:HOPPER_TELEGRAM_TOKEN"\nallow = ["1"]\n')
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("HOPPER_TELEGRAM_TOKEN", "HOPPER_URL", "HOPPER_TOKEN")}
        self.env.update(HOPPER_CONFIG=cfg, HOPPER_DB=os.path.join(self._tmp.name, "h.db"),
                        HOME=self._tmp.name, PATH=self._tmp.name)

    def test_failed_send_exits_non_zero_with_a_clear_error(self):
        for args, why in ((["send", "telegram:1", "hi"], "HOPPER_TELEGRAM_TOKEN, which is not set"),
                          (["send", "telegram:2", "hi"], "HOPPER_TELEGRAM_TOKEN, which is not set"),
                          (["send", "nope:1", "hi"], "no [channels.nope]")):
            r = subprocess.run([sys.executable, "-m", "hopper", *args], env=self.env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(why, r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_flush_exit_status_follows_failures(self):
        from hopper import cli
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch("builtins.print"):
            with mock.patch.object(Hopper, "flush", return_value={"sent": 1, "failed": 2}):
                self.assertEqual(cli.main(["flush"]), 1)
            with mock.patch.object(Hopper, "flush", return_value={"sent": 1, "failed": 0}):
                self.assertEqual(cli.main(["flush"]), 0)


if __name__ == "__main__":
    unittest.main()
