import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

from hopper import Hopper, HopperError, RemoteHopper
from hopper.config import Config, Token
from hopper.worker import Runner


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cfg = Config(db=os.path.join(cls._tmp.name, "h.db"),
                     file_dir=os.path.join(cls._tmp.name, "n"),
                     tokens=[Token("phone", "p" * 20, ("produce",)),
                             Token("box", "w" * 20, ("work",))])
        cls.hop = Hopper(config=cfg)
        cls.port = free_port()
        from hopper.server import serve
        threading.Thread(target=serve, args=(cls.hop,), daemon=True,
                         kwargs={"port": cls.port, "quiet": True, "housekeeping_s": 0.2}).start()
        url = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.05)
        cls.phone = RemoteHopper(url, "p" * 20)
        cls.box = RemoteHopper(url, "w" * 20)
        cls.stranger = RemoteHopper(url, "x" * 20)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_roundtrip_and_scopes(self):
        job = self.phone.add(title="from phone", priority="urgent")
        self.assertEqual(job["source"], "phone")
        with self.assertRaises(HopperError):
            self.phone.claim(worker="sneaky")  # produce scope can't work
        with self.assertRaises(HopperError):
            self.stranger.list()
        got = self.box.claim(worker="pi")
        self.assertEqual(got["id"], job["id"])
        self.assertEqual(got["lease_owner"], "box/pi")  # namespaced by token
        done = self.box.complete(job_id=job["id"], worker="pi", summary="ok", evidence="checked")
        self.assertEqual(done["status"], "done")
        self.assertEqual(self.phone.show(job_id=job["id"])["result"]["summary"], "ok")

    def test_internal_args_stripped(self):
        job = self.phone.add(title="x")
        shown = self.phone._call("show", job_id=job["id"], _db="nope")
        self.assertEqual(shown["id"], job["id"])

    def test_refuses_no_tokens_off_localhost(self):
        from hopper.server import serve
        bare = Hopper(config=Config(db=os.path.join(self._tmp.name, "b.db")))
        with self.assertRaises(HopperError):
            serve(bare, host="0.0.0.0", port=free_port(), no_auth=True)
        with self.assertRaises(HopperError):
            serve(bare, port=free_port())  # no tokens and no --no-auth
        bare.close()


class Mcp(unittest.TestCase):
    def test_tools_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HOPPER_DB"] = os.path.join(tmp, "h.db")
            os.environ["HOPPER_CONFIG"] = os.path.join(tmp, "none.toml")
            os.environ["HOPPER_WORKER"] = "agent-1"
            os.environ.pop("HOPPER_URL", None)
            try:
                msgs = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                        "name": "hop_add", "arguments": {"title": "check the site", "priority": "high",
                                                         "done_when": "HTTP 200 seen"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "hop_claim", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "hop_complete", "arguments": {"job_id": "BAD", "summary": "x"}}},
                    {"jsonrpc": "2.0", "id": 6, "method": "nope"},
                ]
                out = io.StringIO()
                from hopper.mcp import run
                run(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out)
                replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
            finally:
                for k in ("HOPPER_DB", "HOPPER_CONFIG", "HOPPER_WORKER"):
                    os.environ.pop(k, None)
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(replies), 6)  # the notification got no reply
        names = {t["name"] for t in replies[2]["result"]["tools"]}
        self.assertIn("hop_split", names)
        claimed = json.loads(replies[4]["result"]["content"][0]["text"])
        self.assertEqual(claimed["lease_owner"], "agent-1")
        self.assertIn("## Done when\nHTTP 200 seen", claimed["brief"])
        self.assertTrue(replies[5]["result"]["isError"])
        self.assertEqual(replies[6]["error"]["code"], -32601)


class Work(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "h.db")
        self.hop = Hopper(config=Config(db=self.db, file_dir=os.path.join(self._tmp.name, "n")))

    def tearDown(self):
        self.hop.close()
        self._tmp.cleanup()

    def run_once(self, command):
        return Runner(self.hop, command, name="t", once=True, poll=0.05,
                      log_dir=os.path.join(self._tmp.name, "logs")).run()

    def test_output_becomes_result(self):
        j = self.hop.add("say hi")
        self.run_once(f"{sys.executable} -c \"import sys; b=sys.stdin.read(); print('saw brief' if 'say hi' in b else 'no brief')\"")
        job = self.hop.show(j["id"])
        self.assertEqual(job["status"], "done")
        self.assertIn("saw brief", job["result"]["summary"])

    def test_agent_reports_itself(self):
        j = self.hop.add("self report")
        env_cli = f"{sys.executable} -m hopper done $HOPPER_JOB_ID -m reported -e 'ran it'"
        Runner(self.hop, env_cli, name="t", shell=True, once=True, poll=0.05,
               log_dir=os.path.join(self._tmp.name, "logs"),
               env_extra={"HOPPER_CONFIG": os.path.join(self._tmp.name, "none.toml"),
                          "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}).run()
        job = self.hop.show(j["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["summary"], "reported")

    def test_nonzero_exit_retries(self):
        j = self.hop.add("will fail")
        self.run_once(f"{sys.executable} -c \"raise SystemExit(3)\"")
        job = self.hop.show(j["id"])
        self.assertEqual(job["status"], "queued")
        self.assertIn("exited 3", self.hop.events(j["id"])[-1]["reason"])

    def test_idle_once_exits(self):
        self.assertEqual(self.run_once("true"), 0)


if __name__ == "__main__":
    unittest.main()
