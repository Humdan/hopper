import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

from hopper import client, cluster, mainframe
from hopper.core import Hopper
from hopper.config import load
from hopper.server import serve
from hopper.util import HopperError
from tests.test_interfaces import free_port


@contextlib.contextmanager
def device(root, name):
    """Act as a separate machine: its own HOME, config and database."""
    home = os.path.join(root, name)
    os.makedirs(home, exist_ok=True)
    env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, "cfg"), "XDG_DATA_HOME": os.path.join(home, "data")}
    with mock.patch.dict(os.environ, env):
        for k in ("HOPPER_URL", "HOPPER_TOKEN", "HOPPER_DB", "HOPPER_CONFIG"):
            os.environ.pop(k, None)
        yield home


class Cluster(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = cls._tmp.name
        cls.port = free_port()
        with device(cls.root, "main"):
            cls.info = mainframe.setup("home", port=cls.port)
            cls.hop = Hopper(config=load())
            threading.Thread(target=serve, args=(cls.hop,), kwargs={"port": cls.port, "quiet": True,
                                                                    "housekeeping_s": 0.2}, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.url + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.05)

    def test_setup_is_repeatable_and_health_names_the_cluster(self):
        with device(self.root, "main"):
            again = mainframe.setup("other", port=self.port)
            self.assertFalse(again["created"])
            self.assertEqual(again["id"], self.info["id"])
        h = json.load(urllib.request.urlopen(self.url + "/health"))
        self.assertEqual((h["cluster"], h["cluster_id"]), ("home", self.info["id"]))
        self.assertEqual(oct(os.stat(os.path.join(self.root, "main", "cfg", "hopper", "tokens.toml")).st_mode)[-3:], "600")

    def test_invite_needs_a_reachable_address(self):
        with device(self.root, "main"), self.assertRaises(HopperError):
            mainframe.invite("laptop")   # set up on loopback with no --advertise

    def test_client_joins_and_shows_up(self):
        with device(self.root, "main"):
            code = mainframe.invite("laptop", allow_local=True)
        with device(self.root, "laptop") as home:
            r = client.join(code)
            self.assertEqual((r["cluster"], r["name"], r["self"]), ("home", "laptop", False))
            settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
            self.assertEqual(sorted(settings["hooks"]), sorted(client.HOOK_EVENTS))
            self.assertTrue(settings["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith(" pulse"))
            client.install_hooks()   # twice must not duplicate
            settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
            self.assertEqual(len(settings["hooks"]["Stop"]), 1)
            self.assertIsInstance(client.connect(), client.RemoteHopper)   # CLI now talks to the mainframe
            # a second cluster can't be joined without leaving
            other = cluster.encode_join("http://x", "elsewhere", "f" * 32, "t" * 20, "laptop")
            with self.assertRaises(HopperError):
                client.join(other)
            # the device is not a mainframe of the cluster it belongs to as a client
            with self.assertRaises(HopperError):
                mainframe.setup("home")
            self.assertTrue(client.leave())
            settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
            self.assertEqual(settings.get("hooks"), {})
            self.assertEqual(cluster.load_role(), {})

    def test_wrong_mainframe_is_refused(self):
        code = cluster.encode_join(self.url, "home", "0" * 32, "t" * 20, "laptop")   # right address, wrong cluster id
        with device(self.root, "stranger"), self.assertRaises(HopperError) as cm:
            client.join(code)
        self.assertIn("exactly one mainframe", str(cm.exception))

    def test_bad_codes(self):
        for bad in ("", "nope", "hop1.@@@", cluster.CODE_PREFIX + "e30"):
            with self.assertRaises(HopperError):
                cluster.decode_join(bad)

    def test_a_device_can_be_both(self):
        with device(self.root, "both"):
            info = mainframe.setup("solo", port=free_port(), advertise="http://localhost:1")
            with self.assertRaises(HopperError):
                mainframe.invite("nope")   # loopback address: other devices can't use it
            code = mainframe.invite("self", allow_local=True)
            role = cluster.load_role()
            role["client"] = {"cluster": "solo", "id": info["id"], "url": "http://localhost:1", "token": "t" * 20,
                              "name": "self", "self": True}
            cluster.save_role(role)
            self.assertNotIsInstance(client.connect(), client.RemoteHopper)   # uses its own database directly
            self.assertTrue(code.startswith("hop1."))
