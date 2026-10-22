import unittest

from hopper import Hopper
from hopper.config import Config


class Routing(unittest.TestCase):
    def setUp(self):
        self.hop = Hopper(db=":memory:", config=Config(db=":memory:"))

    def test_nobody_serves_the_queue_warns(self):
        job = self.hop.add("lights", queue="default")
        self.assertIn("no live worker serves queue 'default'", job["warnings"][0])

    def test_warning_points_at_the_queue_that_has_the_capability(self):
        self.hop.claim("pi/led", caps="shell,leds", queues="piclaw")
        job = self.hop.add("lights", queue="hermes", requires="leds")
        self.assertIn("serve: piclaw", job["warnings"][0])

    def test_wrong_caps_on_a_served_queue_warns(self):
        self.hop.claim("box/a", caps="shell", queues="hermes")
        job = self.hop.add("lights", queue="hermes", requires="leds")
        self.assertIn("has leds", job["warnings"][0])

    def test_no_warning_when_a_worker_can_take_it(self):
        self.hop.claim("pi/led", caps="shell,leds", queues="piclaw,auto")
        self.assertNotIn("warnings", self.hop.add("lights", queue="auto", requires="leds"))

    def test_auto_queue_goes_only_to_a_capable_worker(self):
        self.hop.claim("pi/led", caps="shell,leds", queues="auto")
        self.hop.claim("herm/full", caps="shell", queues="auto")
        j = self.hop.add("lights", queue="auto", requires="leds")
        self.assertIsNone(self.hop.claim("herm/full", caps="shell", queues="auto"))
        self.assertEqual(self.hop.claim("pi/led", caps="shell,leds", queues="auto")["id"], j["id"])

    def test_sessions_do_not_count_as_workers(self):
        self.hop.ping("mac/claude", doing="idle", kind="session", caps=["leds"], queues=["auto"])
        self.assertIn("warnings", self.hop.add("lights", queue="auto", requires="leds"))

    def test_overview_lists_unserved_queued_jobs(self):
        j = self.hop.add("stuck", queue="nowhere")
        self.assertIn(j["id"], self.hop.overview()["unserved"])


if __name__ == "__main__":
    unittest.main()
