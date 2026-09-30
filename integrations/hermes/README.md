# Hopper with Hermes Agent

## 1. Tools (MCP)

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  hopper:
    command: hop
    args: [mcp]
    enabled: true
```

Restart the gateway when nothing is mid-run, then check with `hermes mcp test hopper`.
To limit a platform's tools (e.g. a phone-facing API profile), list `hopper` in that
platform's toolset allowlist.

## 2. Skill

```sh
mkdir -p ~/.hermes/skills/productivity/hopper
cp integrations/SKILL.md ~/.hermes/skills/productivity/hopper/SKILL.md
```

## 3. Messages become jobs

The usual pattern: quick questions stay in chat; requests that are real work ("fix X", "find
me Y by tonight") become jobs with the chat as the reply address, so the result comes back
to the same conversation when it's done:

```
hop_add(title="...", body="...", priority="urgent", reply_to="telegram:<chat id>")
```

The Telegram sink needs the chat allow-listed in Hopper's config and
`HOPPER_TELEGRAM_TOKEN` set for the process that flushes notices (`hop serve` or `hop work`).

## 4. Hermes as a worker

```sh
hop work --name hermes --caps shell,git,web --shell \
  --exec 'hermes -z "$(cat {prompt_file})"'
```
