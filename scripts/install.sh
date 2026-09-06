#!/usr/bin/env bash
# Links the scripts into ~/.local/bin, the autostart entry into ~/.config/autostart,
# and registers the SessionStart/SessionEnd hooks in ~/.claude/settings.json.
# Safe to run again after a git pull.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="$HOME/.local/bin"
autostart_dir="$HOME/.config/autostart"
settings="$HOME/.claude/settings.json"

mkdir -p "$bin_dir" "$autostart_dir" "$(dirname "$settings")"
ln -sfn "$repo/bin/claude-session-registry" "$bin_dir/claude-session-registry"
ln -sfn "$repo/bin/claude-resume-crashed" "$bin_dir/claude-resume-crashed"
ln -sfn "$repo/autostart/claude-resume-crashed.desktop" "$autostart_dir/claude-resume-crashed.desktop"

python3 - "$settings" <<'PY'
import json, sys
path = sys.argv[1]
try:
    settings = json.load(open(path))
except FileNotFoundError:
    settings = {}
hooks = settings.setdefault("hooks", {})
for event, action in (("SessionStart", "start"), ("SessionEnd", "end")):
    command = f"$HOME/.local/bin/claude-session-registry {action} $PPID"
    entries = hooks.setdefault(event, [])
    already = any(h.get("command") == command for e in entries for h in e.get("hooks", []))
    if not already:
        entries.append({"hooks": [{"type": "command", "command": command}]})
with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PY

echo "installed. Sessions started from now on are registered; run 'claude-resume-crashed --list' to check."
