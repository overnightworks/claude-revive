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
import json, os, sys
path = sys.argv[1]
# Parsing comes before anything is written: a settings file this cannot read is
# the operator's to repair, and the install refuses rather than replacing it.
try:
    with open(path) as f:
        settings = json.load(f)
except FileNotFoundError:
    settings = {}
hooks = settings.setdefault("hooks", {})
for event, action in (("SessionStart", "start"), ("SessionEnd", "end")):
    command = f"$HOME/.local/bin/claude-session-registry {action} $PPID"
    entries = hooks.setdefault(event, [])
    already = any(h.get("command") == command for e in entries for h in e.get("hooks", []))
    if not already:
        entries.append({"hooks": [{"type": "command", "command": command}]})
# The file is replaced by a rename rather than rewritten in place, so a crash,
# a full disk or a closed terminal leaves the operator's original settings
# untouched instead of truncated. The temporary sibling shares the directory
# because a rename is atomic only within one filesystem, and its fixed name is
# overwritten by the next run rather than accumulating.
being_written = path + ".installing"
with open(being_written, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(being_written, path)
PY

echo "installed. Sessions started from now on are registered; run 'claude-resume-crashed --list' to check."
