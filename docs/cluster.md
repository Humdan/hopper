# Getting started with a cluster

Use this when you want several devices (a Mac, a Linux box, a Raspberry Pi) to share one Hopper queue and one
dashboard. If you only have one machine, skip this page: `pipx install ...` then `hop init` is all you need.

## The idea

- A **cluster** is the group of devices that share one queue.
- It has exactly **one mainframe**: the device that holds the queue and serves the dashboard. Pick one that is
  usually on.
- Every other device is a **client**. It reports what its agents are doing and uses the same queue.
- One device can be both (the mainframe is normally also a client of its own cluster).

## Before you start

| You need | Why |
|---|---|
| Python 3.11 or newer on every device | Hopper is pure Python with no other dependencies |
| `pipx` (recommended) or `pip` | to install it. `pipx`: `brew install pipx` (macOS) or `sudo apt install pipx` (Debian/Ubuntu) |
| A way for the devices to reach each other | the same LAN, or a VPN such as Tailscale. The mainframe needs an address the others can use |

Install on **every** device (mainframe and clients):

```sh
pipx install git+https://github.com/Humdan/hopper      # or: pip install git+https://github.com/Humdan/hopper
hop --version
```

## Step 1: set up the mainframe

On the device that will hold the queue:

```sh
hop setup mainframe --cluster home --host <address> --service
```

- `--host` is the address the **other devices** will use to reach this one: its LAN IP or VPN IP (find it with
  `ip addr`, `ifconfig` or `tailscale ip -4`). It is not `127.0.0.1`, which only works on this machine.
  If clients should use a different URL (a hostname, a reverse proxy), add `--advertise https://hopper.example.com`.
- `--service` keeps it running in the background and starts it at login (a systemd user unit on Linux; a launchd
  agent on macOS). Without it, run `hop serve --host <address>` yourself.

You will see the cluster name, its URL and a **dashboard token**. Open the URL in a browser and paste the token to see
the dashboard. The token is also saved in `~/.config/hopper/tokens.toml` (readable only by you).

## Step 2: add each other device

On the mainframe, once per device:

```sh
hop invite laptop
```

It prints a **join code** and the two commands to run on the laptop. The code contains a secret, so send it privately
and use it once.

On the laptop (after installing Hopper as above):

```sh
hop setup client --join <code>
```

This checks it reached the right mainframe, saves the membership, and sets up Claude Code with three things: a hook so
your sessions show on the dashboard, the `hop_*` tools (an MCP server) so Claude can queue and take jobs, and a skill
that teaches Claude what Hopper is. Fully quit and reopen Claude Code, start a new session, and send a prompt: within a
few seconds the laptop appears under **Sessions**, and you can ask Claude "what is Hopper?". From now on `hop add`, `hop ls` and the rest on the laptop talk to the mainframe.

Already-open Claude Code sessions don't appear until you restart them.

## Everyday commands

| Command | What it does |
|---|---|
| `hop setup status` | what this device is (mainframe, client, both, or neither) |
| `hop setup` | explains the two roles and what to run |
| `hop invite <name>` | (mainframe) a join code for another device |
| `hop setup client --leave` | leave the cluster and remove the hooks |
| `hop setup agents` | (re)install Claude Code's hook, tools and skill on a device that already joined |
| `hop setup client --join <code> --no-agents` | join without touching Claude Code's settings |

## One mainframe per cluster

The mainframe creates the cluster's id and every join code carries it. A client refuses any server whose id differs, so
it can't be tricked into joining a lookalike. A device that is a client of a cluster refuses to set up a second
mainframe for it (use `--cluster` with a different name to start a separate cluster), and a device is a client of one
cluster at a time: `hop setup client --leave` before joining another.

New invites work without restarting the server.

## Updating

On any device:

```sh
pipx upgrade hopper-queue          # or: pip install --upgrade git+https://github.com/Humdan/hopper
hop --version
```

Updating changes only the program. Your cluster membership (`role.json`), tokens, config and the queue itself live in
`~/.config/hopper` and `~/.local/share/hopper` and are left alone, and the Claude Code hooks keep working. After
updating the **mainframe**, restart its service so the server runs the new code (`systemctl --user restart
hopper-mainframe` on Linux, or stop and start `hop serve`). Clients and the mainframe can be updated in any order.

If an update doesn't seem to change anything, force it: `pipx install --force git+https://github.com/Humdan/hopper`.
Releases bump the version number, so a normal upgrade picks them up.

## Troubleshooting

| You see | What it means and what to do |
|---|---|
| `can't reach the mainframe at ...` | the client can't open that address. Check the mainframe is running (`hop setup status`, then open its URL in a browser), that both devices are on the same network or VPN, and that a firewall isn't blocking the port (default 8770) |
| `... is not the mainframe this code was made for` | the address now answers with a different cluster id. The mainframe was reset or replaced; ask for a fresh code from the real one |
| `the mainframe answered HTTP 401` | the token in the code was revoked or removed. Run `hop invite` again |
| `the mainframe's address is http://127.0.0.1... which other devices can't reach` | set up the mainframe with `--host <reachable address>` (or `--advertise URL`). To test on one machine, use `hop invite --local` |
| `this device is already a client of cluster ...` | leave first with `hop setup client --leave` |
| `... isn't valid JSON` (about `~/.claude/settings.json`) | fix or move that file aside, then run the join again |
| Claude says it doesn't know what Hopper is | the device joined before 0.5.2, or `claude` wasn't on PATH at the time. Update (`pipx upgrade hopper-queue`), run `hop setup agents`, then fully quit and reopen Claude Code |
| the device joined but doesn't show under Sessions | restart Claude Code (open sessions don't pick up new hooks), then send a prompt. `hop pulse` is the hook; it never fails a session, so errors are silent |

## Where things live

| File | Holds |
|---|---|
| `~/.config/hopper/role.json` | what this device is; on a client it includes its token (mode 600) |
| `~/.config/hopper/tokens.toml` | on the mainframe, the tokens handed out by `hop invite` (mode 600) |
| `~/.config/hopper/config.toml` | optional hand-written settings (queues, sinks, extra tokens) |
| `~/.local/share/hopper/hopper.db` | on the mainframe, the queue itself |
