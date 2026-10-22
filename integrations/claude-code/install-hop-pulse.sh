#!/bin/sh
# Install the hop-pulse hook for Claude Code on this machine.
#
#   HOPPER_URL=https://queue.example.com HOPPER_TOKEN=... sh integrations/claude-code/install-hop-pulse.sh
#
# Without those variables it asks for them (the token is not echoed). Needs python3. Re-running is safe;
# your settings.json is backed up to settings.json.prehop first.
set -e
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin" "$HOME/.config/hopper" "$HOME/.claude"

if [ -z "$HOPPER_URL" ]; then printf 'Hopper URL: '; read -r HOPPER_URL; fi
if [ -z "$HOPPER_TOKEN" ]; then printf 'Hopper token (a "work" token): '; stty -echo 2>/dev/null || true; read -r HOPPER_TOKEN; stty echo 2>/dev/null || true; echo; fi

cp "$here/hop-pulse" "$HOME/.local/bin/hop-pulse"
chmod +x "$HOME/.local/bin/hop-pulse"
umask 077
printf 'HOPPER_URL=%s\nHOPPER_TOKEN=%s\n' "$HOPPER_URL" "$HOPPER_TOKEN" > "$HOME/.config/hopper/pulse.env"

[ -f "$HOME/.claude/settings.json" ] && cp "$HOME/.claude/settings.json" "$HOME/.claude/settings.json.prehop" \
  || echo '{}' > "$HOME/.claude/settings.json"
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
d = json.load(open(p))
hooks = d.setdefault("hooks", {})
cmd = {"type": "command", "command": "~/.local/bin/hop-pulse", "timeout": 5}
for ev in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"):
    groups = hooks.setdefault(ev, [])
    if not any(c.get("command", "").endswith("hop-pulse") for g in groups for c in g.get("hooks", [])):
        groups.append({"matcher": "*", "hooks": [cmd]} if ev == "PostToolUse" else {"hooks": [cmd]})
json.dump(d, open(p, "w"), indent=2)
PY

echo '{"hook_event_name":"SessionStart","cwd":"'"$HOME"'","session_id":"install00"}' | "$HOME/.local/bin/hop-pulse"
echo "Installed. Start a new Claude Code session; it should show up on the Hopper dashboard."
