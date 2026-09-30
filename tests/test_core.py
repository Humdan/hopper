import json
import os
import tempfile
import threading
import unittest

from hopper import Hopper, HopperError
from hopper.config import Config, QueuePolicy


def make(tmp, **cfg) -> Hopper:
    c = Config(db=os.path.join(tmp, "h.db"), file_dir=os.path.join(tmp, "notices"), **cfg)
    return Hopper(config=c)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.h = make(self.tmp)

    def tearDown(self):
        self.h.close()
        self._tmp.cleanup()

    def expire(self, job_id):
        self.h._db.execute("UPDATE jobs SET lease_expires = 0 WHERE id = ?", (job_id,))


class Ordering(Base):
    def test_priority_then_age(self):
        a = self.h.add("a", priority="low")
        b = self.h.add("b", priority="urgent")
        c = self.h.add("c", priority="urgent")
        order = [self.h.claim("w")["id"] for _ in range(3)]
        self.assertEqual(order, [b["id"], c["id"], a["id"]])
        self.assertIsNone(self.h.claim("w"))

    def test_aging_lifts_starving_work(self):
        old = self.h.add("old", priority=40)
        new = self.h.add("new", priority=50)
        # waited 20 hours: +20 (the cap) beats a fresh +10 gap
        self.h._db.execute("UPDATE jobs SET created_at = created_at - 72000 WHERE id = ?", (old["id"],))
        self.assertEqual(self.h.claim("w")["id"], old["id"])
        self.assertEqual(self.h.claim("w")["id"], new["id"])

    def test_min_priority_reserves_slot(self):
        self.h.add("background", priority="background")
        self.assertIsNone(self.h.claim("w", min_priority="high"))
        u = self.h.add("urgent", priority="urgent")
        self.assertEqual(self.h.claim("w", min_priority="high")["id"], u["id"])

    def test_not_before(self):
        self.h.add("later", not_before="+1h")
        self.assertIsNone(self.h.claim("w"))

    def test_queues_are_separate(self):
        self.h.add("x", queue="ops")
        self.assertIsNone(self.h.claim("w"))
        self.assertIsNotNone(self.h.claim("w", queues=["ops"]))


class Capabilities(Base):
    def test_requires_subset(self):
        j = self.h.add("git work", requires="shell,git")
        self.assertIsNone(self.h.claim("w", caps=["shell"]))
        self.assertEqual(self.h.claim("w", caps=["git", "shell", "web"])["id"], j["id"])

    def test_no_requirements_any_worker(self):
        self.h.add("easy")
        self.assertIsNotNone(self.h.claim("w"))


class Leases(Base):
    def test_one_owner(self):
        j = self.h.add("x")
        self.h.claim("w1")
        with self.assertRaises(HopperError):
            self.h.complete(j["id"], "w2", "not mine")

    def test_expired_lease_requeues_then_fails(self):
        j = self.h.add("x", max_attempts=2)
        self.h.claim("w1")
        self.expire(j["id"])
        again = self.h.claim("w2")
        self.assertEqual(again["id"], j["id"])
        self.assertEqual(again["attempts"], 2)
        self.expire(j["id"])
        self.assertIsNone(self.h.claim("w3"))
        self.assertEqual(self.h.show(j["id"])["status"], "failed")

    def test_heartbeat_extends(self):
        j = self.h.add("x")
        before = self.h.claim("w")["lease_expires"]
        after = self.h.heartbeat(j["id"], "w", extend="1h", progress="halfway")["lease_expires"]
        self.assertGreater(after, before)
        self.assertEqual(self.h.events(j["id"])[-1]["kind"], "progress")

    def test_concurrent_claims_never_double(self):
        for i in range(40):
            self.h.add(f"job {i}")
        got, lock = [], threading.Lock()

        def worker(n):
            h = make(self.tmp)  # separate connection, same file
            while True:
                job = h.claim(f"w{n}")
                if not job:
                    break
                with lock:
                    got.append(job["id"])
            h.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 40)
        self.assertEqual(len(set(got)), 40)


class Outcomes(Base):
    def test_complete_needs_summary_and_policy_evidence(self):
        h = make(self.tmp + "/e", queues={"default": QueuePolicy(require_evidence=True)})
        j = h.add("x")
        h.claim("w")
        with self.assertRaises(HopperError):
            h.complete(j["id"], "w", "")
        with self.assertRaises(HopperError):
            h.complete(j["id"], "w", "did it")
        done = h.complete(j["id"], "w", "did it", evidence="ran tests: 12 passed")
        self.assertEqual(done["result"]["evidence"], "ran tests: 12 passed")
        h.close()

    def test_fail_retries_with_backoff_then_dies(self):
        j = self.h.add("x", max_attempts=2)
        self.h.claim("w")
        r = self.h.fail(j["id"], "w", "flaky")
        self.assertEqual(r["status"], "queued")
        self.assertGreater(r["not_before"], r["updated_at"])
        self.h._db.execute("UPDATE jobs SET not_before = NULL WHERE id = ?", (j["id"],))
        self.h.claim("w")
        self.assertEqual(self.h.fail(j["id"], "w", "still broken")["status"], "failed")

    def test_release_refunds_attempt(self):
        j = self.h.add("x")
        self.h.claim("w")
        self.assertEqual(self.h.release(j["id"], "w")["attempts"], 0)

    def test_retry_and_cancel(self):
        j = self.h.add("x", max_attempts=1)
        self.h.claim("w")
        self.h.fail(j["id"], "w", "no")
        self.assertEqual(self.h.retry(j["id"])["status"], "queued")
        self.assertEqual(self.h.cancel(j["id"], "never mind")["status"], "cancelled")
        with self.assertRaises(HopperError):
            self.h.cancel(j["id"])


class Idempotency(Base):
    def test_key_dedupes_while_open(self):
        a = self.h.add("fix ticker", key="ticker")
        b = self.h.add("fix ticker again", key="ticker")
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b.get("deduplicated"))
        self.h.claim("w")
        self.h.complete(a["id"], "w", "fixed")
        c = self.h.add("fix ticker", key="ticker")  # finished: a new one is allowed
        self.assertNotEqual(c["id"], a["id"])


class Dependencies(Base):
    def test_waits_for_dependency(self):
        first = self.h.add("first", priority="low")
        second = self.h.add("second", priority="urgent", depends_on=[first["id"]])
        self.assertEqual(self.h.claim("w")["id"], first["id"])
        self.assertIsNone(self.h.claim("w"))
        self.h.complete(first["id"], "w", "ok")
        self.assertEqual(self.h.claim("w")["id"], second["id"])

    def test_failed_dependency_fails_dependents(self):
        first = self.h.add("first", max_attempts=1)
        second = self.h.add("second", depends_on=[first["id"]])
        self.h.claim("w")
        self.h.fail(first["id"], "w", "boom")
        s = self.h.show(second["id"])
        self.assertEqual(s["status"], "failed")

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(HopperError):
            self.h.add("x", depends_on=["j_nope"])


class Splitting(Base):
    def test_split_waits_and_resumes_with_results(self):
        parent = self.h.add("big job", priority="high", requires="shell")
        self.h.claim("boss", caps=["shell"])
        split = self.h.split(parent["id"], "boss", [
            {"title": "research", "requires": ["web"]},
            {"title": "write", "depends_on": ["#0"]},
        ])
        self.assertEqual(split["status"], "waiting")
        kids = split["children"]
        self.assertEqual(len(kids), 2)
        # children inherit priority, and ordering is respected
        r = self.h.claim("researcher", caps=["web"])
        self.assertEqual(r["id"], kids[0]["id"])
        self.assertEqual(r["priority"], 70)
        self.assertIsNone(self.h.claim("writer"))
        self.h.complete(r["id"], "researcher", "found 3 sources")
        w = self.h.claim("writer")
        self.assertEqual(w["id"], kids[1]["id"])
        self.assertEqual(self.h.show(parent["id"])["status"], "waiting")
        self.h.complete(w["id"], "writer", "draft written")
        back = self.h.claim("boss", caps=["shell"])
        self.assertEqual(back["id"], parent["id"])
        self.assertEqual([c["result"]["summary"] for c in back["children"]],
                         ["found 3 sources", "draft written"])

    def test_split_is_atomic(self):
        parent = self.h.add("big")
        self.h.claim("boss")
        with self.assertRaises(HopperError):
            self.h.split(parent["id"], "boss", [{"title": "ok"}, {"title": "bad", "depends_on": ["#5"]}])
        self.assertEqual(self.h.show(parent["id"])["children"], [])
        self.assertEqual(self.h.show(parent["id"])["status"], "running")

    def test_cancel_cascades_to_children(self):
        parent = self.h.add("big")
        self.h.claim("boss")
        kids = self.h.split(parent["id"], "boss", [{"title": "a"}, {"title": "b"}])["children"]
        self.h.cancel(parent["id"])
        for k in kids:
            self.assertEqual(self.h.show(k["id"])["status"], "cancelled")


class Questions(Base):
    def test_ask_answer_roundtrip(self):
        j = self.h.add("book dinner", reply_to="file:me.jsonl")
        self.h.claim("w")
        asked = self.h.ask(j["id"], "w", "Which night?")
        self.assertEqual(asked["status"], "needs_input")
        self.assertIsNone(self.h.claim("w2"))  # parked
        self.h.answer(j["id"], "Friday")
        back = self.h.claim("w2")
        self.assertEqual(back["id"], j["id"])
        self.assertEqual([t["kind"] for t in back["thread"]], ["question", "answer"])
        self.assertEqual(back["attempts"], 1)  # asking didn't burn an attempt
        self.h.flush()
        with open(os.path.join(self.tmp, "notices", "me.jsonl")) as fh:
            notice = json.loads(fh.readline())
        self.assertEqual(notice["event"], "needs_input")
        self.assertEqual(notice["text"], "Which night?")


class Sinks(Base):
    def test_file_sink_and_confinement(self):
        j = self.h.add("x", reply_to="file:out.jsonl")
        self.h.claim("w")
        self.h.complete(j["id"], "w", "all good")
        self.assertEqual(self.h.flush(), {"sent": 1, "failed": 0})
        with self.assertRaises(HopperError):
            self.h.add("x", reply_to="file:/etc/passwd")
        with self.assertRaises(HopperError):
            self.h.add("x", reply_to="file:../../escape.jsonl")

    def test_unlisted_targets_refused(self):
        for target in ("telegram:123", "webhook:https://evil.example/", "hook:rm", "sms:1"):
            with self.assertRaises(HopperError, msg=target):
                self.h.add("x", reply_to=target)

    def test_failed_delivery_retries_later(self):
        h = make(self.tmp + "/w", webhook_allow=("http://127.0.0.1:9/",))
        j = h.add("x", reply_to="webhook:http://127.0.0.1:9/nothing-listens")
        h.claim("w")
        h.complete(j["id"], "w", "done")
        self.assertEqual(h.flush(), {"sent": 0, "failed": 1})
        self.assertEqual(h.flush(), {"sent": 0, "failed": 0})  # backing off, not hammering
        self.assertEqual(h.stats()["outbox_pending"], 1)
        h.close()


class Housekeeping(Base):
    def test_gc_keeps_open_and_recent(self):
        done = self.h.add("old")
        self.h.claim("w")
        self.h.complete(done["id"], "w", "ok")
        self.h.add("still open")
        self.h._db.execute("UPDATE jobs SET finished_at = 0 WHERE id = ?", (done["id"],))
        self.assertEqual(self.h.gc()["removed"], 1)
        self.assertEqual(len(self.h.list(status="all")), 1)

    def test_list_filters(self):
        self.h.add("a", tags="x,y")
        self.h.add("b", tags="y")
        self.assertEqual(len(self.h.list(tags="x")), 1)
        with self.assertRaises(HopperError):
            self.h.list(status="bogus")


if __name__ == "__main__":
    unittest.main()
