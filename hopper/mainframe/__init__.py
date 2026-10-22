"""The mainframe: the one device in a cluster that holds the queue and serves it to the others."""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import uuid
from urllib.parse import urlparse

from .. import cluster
from ..core import Hopper
from ..util import HopperError

DEFAULT_PORT = 8770


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or host.startswith("127.")


def setup(name: str = "home", host: str = "127.0.0.1", port: int = DEFAULT_PORT, advertise: str | None = None,
          service: bool = False) -> dict:
    """Make this device the mainframe of a new cluster. Safe to repeat: it reports what already exists."""
    role = cluster.load_role()
    if role.get("mainframe"):
        return {**role["mainframe"], "created": False}
    client = role.get("client")
    if client and client["cluster"] == name:
        raise HopperError(f"this device is already a client of cluster {name!r} (mainframe {client['url']}), "
                          "and a cluster has exactly one mainframe. Leave it first with `hop setup client --leave`, "
                          "or pick a different cluster name with --cluster.")
    url = (advertise or f"http://{host}:{port}").rstrip("/")
    cluster_id = uuid.uuid4().hex
    hop = Hopper()
    with hop._tx() as db:
        db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('cluster_id', ?), ('cluster', ?)", (cluster_id, name))
    hop.close()
    cluster.save_role({**role, "mainframe": {"cluster": name, "id": cluster_id, "host": host, "port": port, "url": url}})
    dashboard = secrets.token_urlsafe(32)
    cluster.add_token("dashboard", dashboard, ("read", "produce"))
    info = {"cluster": name, "id": cluster_id, "host": host, "port": port, "url": url,
            "dashboard_token": dashboard, "created": True, "service": None}
    if service:
        info["service"] = install_service(host, port)
    return info


def invite(device: str, scopes: tuple[str, ...] = ("work",), allow_local: bool = False) -> str:
    """Mint a token for a device and return the join code it uses to become a client."""
    mf = cluster.load_role().get("mainframe")
    if not mf:
        raise HopperError("this device isn't a mainframe; run `hop setup mainframe` first")
    if not device or not all(c.isalnum() or c in "-_" for c in device):
        raise HopperError("device names use letters, digits, - and _ only")
    if not allow_local and _loopback(urlparse(mf["url"]).hostname or ""):
        raise HopperError(f"the mainframe's address is {mf['url']}, which other devices can't reach. "
                          "Set it up with --host <an address they can reach> or --advertise URL "
                          "(use `hop invite --local` to test on one machine).")
    token = secrets.token_urlsafe(32)
    cluster.add_token(device, token, scopes)
    return cluster.encode_join(mf["url"], mf["cluster"], mf["id"], token, device)


def install_service(host: str, port: int) -> str:
    """Keep the mainframe running: a systemd user unit on Linux, a launchd agent on macOS."""
    hop = shutil.which("hop") or os.path.abspath(sys.argv[0])
    if sys.platform == "darwin":
        path = os.path.expanduser("~/Library/LaunchAgents/org.hopper.mainframe.plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        args = "".join(f"<string>{a}</string>" for a in (hop, "serve", "--host", host, "--port", str(port), "--quiet"))
        with open(path, "w") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
                     '<key>Label</key><string>org.hopper.mainframe</string>'
                     f'<key>ProgramArguments</key><array>{args}</array>'
                     '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n')
        subprocess.run(["launchctl", "load", "-w", path], check=False)
        return path
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    path = os.path.join(unit_dir, "hopper-mainframe.service")
    with open(path, "w") as fh:
        fh.write("[Unit]\nDescription=Hopper mainframe\nAfter=network-online.target\n\n[Service]\n"
                 f"ExecStart={hop} serve --host {host} --port {port} --quiet\nRestart=on-failure\n\n"
                 "[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "hopper-mainframe.service"], check=False)
    return path
