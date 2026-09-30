# Hopper with OpenClaw

Hopper speaks standard MCP over stdio, so OpenClaw connects to it like any local MCP server.
Per the OpenClaw MCP docs (not yet tested against a live install here — reports welcome):

```sh
openclaw mcp add hopper --command hop --arg mcp
openclaw mcp doctor hopper --probe
```

or in `~/.openclaw/openclaw.json`:

```json
{ "mcpServers": { "hopper": { "command": "hop", "args": ["mcp"] } } }
```

Add `HOPPER_URL` / `HOPPER_TOKEN` to the server's environment to use a remote Hopper.

For the protocol, give the agent `integrations/SKILL.md` as a skill.

To run OpenClaw as a worker, point `hop work --exec` at any OpenClaw command that takes a
prompt on stdin or from a file (`{prompt_file}`).
