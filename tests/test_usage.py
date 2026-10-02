import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from hopper import Hopper, HopperError
from hopper.config import Config
from hopper.util import default_project, parse_since
from hopper.worker import Runner, agent_result

CLAUDE_RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "duration_ms": 4200,
    "num_turns": 3, "result": "Fixed the link and pushed.", "total_cost_usd": 0.0421,
    "usage": {"input_tokens": 1200, "output_tokens": 340, "cache_read_input_tokens": 9000,
              "cache_creation_input_tokens": 500},
    "modelUsage": {"claude-sonnet-5-5": {}},
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.h = Hopper(config=Config(db=os.path.join(self.tmp, "h.db"),
                                      file_dir=os.path.join(self.tmp, "n")))

    def tearDown(self):
        self.h.close()
        self._tmp.cleanup()

    def finish(self, title, project=None, usage=None, **kw):
        j = self.h.add(title, project=project, **kw)
        c = self.h.claim("w")
        self.assertEqual(c["id"], j["id"])
        return self.h.complete(j["id"], "w", "ok", usage=usage)


class Projects(Base):
    def test_children_inherit_project(self):
        self.h.add("parent", project="tokentimes")
        p = self.h.claim("w")
        self.h.split(p["id"], "w", [{"title": "a"}, {"title": "b", "project": "other"}])
        kids = {c["title"]: c for c in self.h.list(status="all") if c["parent_id"]}
        self.assertEqual(kids["a"]["project"], "tokentimes")
        self.assertEqual(kids["b"]["project"], "other")

    def test_list_filters_by_project(self):
        self.h.add("x", project="a")
        self.h.add("y", project="b")
        self.assertEqual([j["title"] for j in self.h.list(project="a")], ["x"])

    def test_blank_project_is_none(self):
        self.assertIsNone(self.h.add("x", project="  ")["project"])

    def test_default_project_env_then_git(self):
        old = os.environ.pop("HOPPER_PROJECT", None)
        try:
            os.environ["HOPPER_PROJECT"] = "fromenv"
            self.assertEqual(default_project(), "fromenv")
            os.environ["HOPPER_PROJECT"] = ""
            self.assertIsNone(default_project())
            del os.environ["HOPPER_PROJECT"]
            repo = os.path.join(self.tmp, "myrepo")
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            self.assertEqual(default_project(cwd=repo), "myrepo")
            self.assertIsNone(default_project(cwd=self.tmp))
        finally:
            os.environ.pop("HOPPER_PROJECT", None)
            if old is not None:
                os.environ["HOPPER_PROJECT"] = old


class Usage(Base):
    def test_complete_records_usage(self):
        j = self.finish("x", project="p", usage={"cost_usd": 0.25, "input_tokens": 100,
                                                  "duration_s": 12, "model": "m"})
        (u,) = self.h.usage(j["id"])
        self.assertEqual((u["cost_usd"], u["input_tokens"], u["project"], u["attempt"], u["model"]),
                         (0.25, 100, "p", 1, "m"))
        self.assertEqual(self.h.events(j["id"])[-2]["kind"], "usage")

    def test_bad_usage_refused_and_job_untouched(self):
        j = self.h.add("x")
        self.h.claim("w")
        for bad in ({"cost_usd": -1}, {"cost_usd": "lots"}, {"price": 1}, {"model": "m"}):
            with self.assertRaises(HopperError):
                self.h.complete(j["id"], "w", "ok", usage=bad)
        self.assertEqual(self.h.show(j["id"])["status"], "running")
        self.assertEqual(self.h.usage(j["id"]), [])

    def test_usage_survives_gc(self):
        j = self.finish("x", project="p", usage={"cost_usd": 1.0})
        self.h._db.execute("UPDATE jobs SET finished_at = 0 WHERE id = ?", (j["id"],))
        self.assertEqual(self.h.gc(days=1)["removed"], 1)
        r = self.h.report()
        self.assertEqual(r["rows"][0]["key"], "p")
        self.assertEqual(r["rows"][0]["cost_usd"], 1.0)
        self.assertEqual(r["rows"][0]["jobs"], 0)


class Report(Base):
    def test_by_project_with_retries_and_failures(self):
        self.finish("a", project="tt", usage={"cost_usd": 0.5, "input_tokens": 10, "output_tokens": 5})
        self.finish("b", project="tt", usage={"duration_s": 30})       # unpriced run
        j = self.h.add("c", project="so", max_attempts=2)
        self.h.claim("w")
        self.h.record_usage(j["id"], "w", cost_usd=0.1, duration_s=5)
        self.h.fail(j["id"], "w", "flaky")
        self.h._db.execute("UPDATE jobs SET not_before = NULL WHERE id = ?", (j["id"],))
        self.h.claim("w")
        self.h.record_usage(j["id"], "w", cost_usd=0.2, duration_s=7)
        self.h.fail(j["id"], "w", "gave up")
        r = self.h.report(by="project")
        rows = {g["key"]: g for g in r["rows"]}
        self.assertEqual([g["key"] for g in r["rows"]], ["tt", "so"])   # most expensive first
        self.assertEqual((rows["tt"]["jobs"], rows["tt"]["done"], rows["tt"]["runs"],
                          rows["tt"]["priced_runs"]), (2, 2, 2, 1))
        self.assertEqual((rows["so"]["failed"], rows["so"]["retries"], rows["so"]["run_s"]),
                         (1, 1, 12.0))
        self.assertAlmostEqual(rows["so"]["cost_usd"], 0.3)
        self.assertAlmostEqual(r["total"]["cost_usd"], 0.8)
        self.assertEqual(r["total"]["jobs"], 3)

    def test_window_filters_and_groups(self):
        self.finish("old", project="p", usage={"cost_usd": 9})
        self.h._db.execute("UPDATE jobs SET finished_at = finished_at - 30*86400")
        self.h._db.execute("UPDATE usage SET at = at - 30*86400")
        self.finish("new", project=None, usage={"cost_usd": 1})
        r = self.h.report(since="7d")
        self.assertEqual([(g["key"], g["cost_usd"]) for g in r["rows"]], [("(none)", 1.0)])
        self.assertEqual(len(self.h.report(by="day")["rows"]), 2)
        self.assertEqual(self.h.report(project="p")["total"]["cost_usd"], 9.0)
        self.assertEqual(self.h.report(by="worker")["rows"][0]["key"], "w")

    def test_bad_requests(self):
        with self.assertRaises(HopperError):
            self.h.report(by="colour")
        with self.assertRaises(HopperError):
            self.h.report(since="1d", until="2d")
        self.assertEqual(self.h.report()["rows"], [])

    def test_parse_since_is_in_the_past(self):
        self.assertAlmostEqual(parse_since("7d"), time.time() - 7 * 86400, delta=5)
        self.assertGreater(parse_since("+1h"), time.time())


class Migration(unittest.TestCase):
    def test_v1_database_gains_project_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "old.db")
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE jobs (id TEXT PRIMARY KEY, queue TEXT NOT NULL DEFAULT 'default',
                  title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', done_when TEXT,
                  priority INTEGER NOT NULL DEFAULT 50, status TEXT NOT NULL DEFAULT 'queued',
                  requires TEXT NOT NULL DEFAULT '[]', tags TEXT NOT NULL DEFAULT '[]', source TEXT,
                  reply_to TEXT, key TEXT, parent_id TEXT, not_before REAL,
                  max_attempts INTEGER NOT NULL DEFAULT 3, attempts INTEGER NOT NULL DEFAULT 0,
                  timeout REAL NOT NULL DEFAULT 900, lease_owner TEXT, lease_expires REAL,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL,
                  finished_at REAL, result TEXT, thread TEXT NOT NULL DEFAULT '[]',
                  meta TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
                INSERT INTO meta VALUES ('schema', '1');
                INSERT INTO jobs(id, title, created_at, updated_at) VALUES ('j_old', 'kept', 1, 1);
            """)
            con.close()
            h = Hopper(config=Config(db=db, file_dir=tmp))
            try:
                self.assertIsNone(h.show("j_old")["project"])
                self.assertEqual(h.add("new", project="p")["project"], "p")
                self.assertEqual(h._db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0], "2")
            finally:
                h.close()


class Interfaces(unittest.TestCase):
    def test_mcp_add_defaults_project_and_reports(self):
        import io
        from hopper.mcp import run
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOPPER_DB": os.path.join(tmp, "h.db"), "HOPPER_CONFIG": os.path.join(tmp, "none.toml"),
                   "HOPPER_WORKER": "agent-1", "HOPPER_PROJECT": "orbit"}
            saved = {k: os.environ.get(k) for k in [*env, "HOPPER_URL"]}
            os.environ.update(env)
            os.environ.pop("HOPPER_URL", None)
            try:
                calls = [("hop_add", {"title": "t"}), ("hop_claim", {}),
                         ("hop_report", {"since": "1d"}), ("hop_report", {"by": "colour"})]
                msgs = [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls, 1)]
                out = io.StringIO()
                run(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out)
                replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
                job_id = json.loads(replies[1]["result"]["content"][0]["text"])["id"]
                h = Hopper(config=Config(db=env["HOPPER_DB"], file_dir=tmp))
                h.complete(job_id, "agent-1", "ok", usage={"cost_usd": 0.3})
                self.assertEqual(h.report()["rows"][0]["key"], "orbit")
                h.close()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        self.assertEqual(json.loads(replies[1]["result"]["content"][0]["text"])["project"], "orbit")
        self.assertIn("rows", json.loads(replies[3]["result"]["content"][0]["text"]))
        self.assertTrue(replies[4]["result"]["isError"])

    def test_http_scopes(self):
        from hopper.api import WORKER_OPS, allowed
        self.assertTrue(allowed(["read"], "report"))
        self.assertTrue(allowed(["read"], "usage"))
        self.assertFalse(allowed(["read"], "record_usage"))
        self.assertTrue(allowed(["work"], "record_usage"))
        self.assertIn("record_usage", WORKER_OPS)


class WorkerUsage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.hop = Hopper(config=Config(db=os.path.join(self.tmp, "h.db"),
                                        file_dir=os.path.join(self.tmp, "n")))

    def tearDown(self):
        self.hop.close()
        self._tmp.cleanup()

    def agent(self, code: str) -> str:
        path = os.path.join(self.tmp, "agent.py")
        with open(path, "w") as fh:
            fh.write(code)
        return f"{sys.executable} {path}"

    def run_once(self, command, **kw):
        Runner(self.hop, command, name="t", once=True, poll=0.05,
               log_dir=os.path.join(self.tmp, "logs"), **kw).run()

    def test_claude_json_result_gives_cost_and_summary(self):
        j = self.hop.add("fix link", project="tt")
        self.run_once(self.agent(f"import json, sys\nsys.stdin.read()\nprint('noise')\n"
                                 f"print(json.dumps({CLAUDE_RESULT!r}))\n"))
        job = self.hop.show(j["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["summary"], "Fixed the link and pushed.")
        (u,) = self.hop.usage(j["id"])
        self.assertEqual((u["cost_usd"], u["input_tokens"], u["output_tokens"], u["cache_read_tokens"],
                          u["cache_write_tokens"], u["model"], u["project"]),
                         (0.0421, 1200, 340, 9000, 500, "claude-sonnet-5-5", "tt"))
        self.assertGreater(u["duration_s"], 0)

    def test_claude_error_result_fails_the_attempt(self):
        j = self.hop.add("x")
        bad = {**CLAUDE_RESULT, "is_error": True, "result": "Credit balance is too low"}
        self.run_once(self.agent(f"import json\nprint(json.dumps({bad!r}))\n"))
        job = self.hop.show(j["id"])
        self.assertEqual(job["status"], "queued")
        self.assertIn("Credit balance", self.hop.events(j["id"])[-1]["reason"])
        self.assertEqual(self.hop.usage(j["id"])[0]["cost_usd"], 0.0421)

    def test_usage_file_and_plain_output(self):
        j = self.hop.add("x")
        self.run_once(self.agent("import json, os\nprint('all good')\n"
                                 "json.dump({'cost_usd': 0.07, 'model': 'gpt-x'}, "
                                 "open(os.environ['HOPPER_USAGE_FILE'], 'w'))\n"))
        job = self.hop.show(j["id"])
        self.assertEqual(job["result"]["summary"], "all good")
        (u,) = self.hop.usage(j["id"])
        self.assertEqual((u["cost_usd"], u["model"]), (0.07, "gpt-x"))

    def test_time_recorded_without_cost_and_not_twice(self):
        a = self.hop.add("plain")
        self.run_once(self.agent("print('done')\n"))
        (u,) = self.hop.usage(a["id"])
        self.assertIsNone(u["cost_usd"])
        self.assertIsNotNone(u["duration_s"])
        b = self.hop.add("reports itself")
        cli = (f"{sys.executable} -m hopper done $HOPPER_JOB_ID -m ok -e ran --cost 0.5")
        self.run_once(cli, shell=True, env_extra={
            "HOPPER_CONFIG": os.path.join(self.tmp, "none.toml"),
            "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))})
        runs = self.hop.usage(b["id"])
        self.assertEqual([r["cost_usd"] for r in runs], [0.5])

    def test_agent_result_ignores_unrelated_json(self):
        self.assertIsNone(agent_result('{"hello": 1}\nplain text'))
        self.assertIsNone(agent_result(""))


if __name__ == "__main__":
    unittest.main()
