# Clusters: one mainframe, many clients

A **cluster** is the set of devices that share one queue. It has exactly **one mainframe**, the device that
holds the queue and serves it, and any number of **clients**. One device can be both: the mainframe is
usually also a client of its own cluster.

## Start a cluster (on the mainframe)

```sh
hop setup mainframe --cluster home --host <address your other devices can reach> --service
```

This creates the database, mints the cluster's id, writes a `dashboard` token, and (with `--service`) installs
and starts a background service (systemd user unit on Linux; a launchd agent on macOS, written and loaded).
Open `http://<host>:8770/` and paste the dashboard token.

`--host` must be an address the other devices can reach (a LAN or VPN address, not 127.0.0.1). If it differs
from the URL clients should use, add `--advertise https://hopper.example.com`.

## Add a device (a client)

On the mainframe:

```sh
hop invite laptop        # prints a join code: it holds a secret, so send it privately
```

On the laptop:

```sh
hop setup client --join <code>
```

The client checks it reached the right mainframe, saves its membership (`role.json`, mode 600), registers
the Claude Code `hop pulse` hook, and from then on `hop add`, `hop ls` and the rest talk to the mainframe.
`hop setup status` shows what a device is; `hop setup client --leave` undoes it (hooks included).

## One mainframe per cluster

The mainframe mints the cluster id and every join code carries it. A client refuses any server whose id
differs, and a device that is already a client of a cluster refuses to set up a second mainframe for it
(`--cluster` with another name starts a different cluster). A device belongs to one cluster as a client;
leave before joining another.

New invites work without restarting the server (it re-reads `tokens.toml` when it sees an unknown token).
