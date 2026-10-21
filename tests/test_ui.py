import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from hopper import Hopper, RemoteHopper
from hopper.config import Config, Token
from tests.test_interfaces import free_port


class Presence(unittest.TestCase):
    def setUp(self):
        self.hop = Hopper(db=":memory:", config=Config(db=":memory:"))

    def test_claim_registers_a_worker_even_when_idle(self):
        self.assertIsNone(self.hop.claim("box/idle", caps="shell", queues="a,b"))
        (w,) = self.hop.workers()
        self.assertEqual((w["name"], w["caps"], w["queues"], w["status"]), ("box/idle", ["shell"], ["a", "b"], "online"))

    def test_busy_and_counters(self):
        j = self.hop.add("do it", queue="q")
        self.hop.claim("w", queues="q")
        self.assertEqual(self.hop.workers()[0]["status"], "busy")
        self.assertEqual(self.hop.workers()[0]["running"][0]["id"], j["id"])
        self.hop.complete(j["id"], "w", "ok", evidence="checked")
        w = self.hop.workers()[0]
        self.assertEqual((w["status"], w["jobs_claimed"], w["jobs_done"], w["jobs_failed"]), ("online", 1, 1, 0))
        k = self.hop.add("bad", queue="q", max_attempts=1)
        self.hop.claim("w", queues="q")
        self.hop.fail(k["id"], "w", "nope")
        self.assertEqual(self.hop.workers()[0]["jobs_failed"], 1)

    def test_status_ages_out(self):
        self.hop.claim("w", queues="q")
        self.hop._db.execute("UPDATE workers SET last_seen = last_seen - 120")
        self.assertEqual(self.hop.workers()[0]["status"], "away")
        self.hop._db.execute("UPDATE workers SET last_seen = last_seen - 3600")
        self.assertEqual(self.hop.workers()[0]["status"], "offline")

    def test_overview_shape(self):
        self.hop.add("one", queue="q", source="cli")
        j = self.hop.add("two", queue="q")
        self.hop.claim("w", queues="q")
        self.hop.heartbeat(j["id"] if self.hop.show(j["id"])["status"] == "running" else self.hop.list("running")[0]["id"],
                           "w", progress="halfway")
        o = self.hop.overview()
        self.assertEqual(o["counts"]["running"], 1)
        self.assertEqual(o["counts"]["queued"], 1)
        self.assertEqual(o["queues"]["q"]["queued"], 1)
        running = [x for x in o["jobs"] if x["status"] == "running"][0]
        self.assertEqual(running["progress"], "halfway")
        self.assertEqual(len(o["per_hour"]["done"]), 24)
        self.assertTrue(o["activity"])
        self.assertIn("cli", [s["source"] for s in o["sources"]])
        json.dumps(o, default=str)


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cfg = Config(db=os.path.join(cls._tmp.name, "h.db"), file_dir=os.path.join(cls._tmp.name, "n"),
                     tokens=[Token("watch", "r" * 20, ("read",)), Token("box", "w" * 20, ("work",))])
        cls.hop = Hopper(config=cfg)
        cls.port = free_port()
        from hopper.server import serve
        threading.Thread(target=serve, args=(cls.hop,), daemon=True,
                         kwargs={"port": cls.port, "quiet": True, "housekeeping_s": 0.2}).start()
        cls.url = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.url + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.05)

    def get(self, path, token=None):
        req = urllib.request.Request(self.url + path, headers={"Authorization": f"Bearer {token}"} if token else {})
        return urllib.request.urlopen(req, timeout=5)

    def test_page_is_public_but_carries_no_data(self):
        r = self.get("/")
        body = r.read().decode()
        self.assertIn("text/html", r.headers["Content-Type"])
        self.assertIn("Hopper", body)
        self.assertNotIn("j_", body.split("<script>")[0])

    def test_data_needs_a_token(self):
        for path in ("/v1/overview", "/v1/stream", "/v1/workers"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path)
            self.assertEqual(cm.exception.code, 401)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path, "x" * 20)
            self.assertEqual(cm.exception.code, 401)

    def test_overview_for_a_read_token_and_worker_presence(self):
        box = RemoteHopper(self.url, "w" * 20)
        self.assertIsNone(box.claim(worker="sonnet", caps="shell", queues="ui"))
        o = json.load(self.get("/v1/overview", "r" * 20))["result"]
        names = [w["name"] for w in o["workers"]]
        self.assertIn("box/sonnet", names)
        w = [w for w in o["workers"] if w["name"] == "box/sonnet"][0]
        self.assertEqual(w["addr"], "127.0.0.1")

    def test_stream_pushes_a_snapshot_then_a_change(self):
        r = self.get("/v1/stream", "r" * 20)
        self.assertEqual(r.headers["Content-Type"], "text/event-stream")

        def event():
            lines = []
            while True:
                line = r.readline().decode()
                if line == "\n" and lines:
                    break
                if line != "\n":
                    lines.append(line)
            return json.loads([l for l in lines if l.startswith("data: ")][0][6:])

        first = event()
        self.assertIn("counts", first)
        n = first["counts"]["queued"]
        self.hop.add("pushed", queue="ui-stream")
        for _ in range(8):
            snap = event()
            if snap["counts"]["queued"] == n + 1:
                break
        self.assertEqual(snap["counts"]["queued"], n + 1)
        r.close()


if __name__ == "__main__":
    unittest.main()
