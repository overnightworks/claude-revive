# claude-revive

Brings every Claude Code session back after a PC crash: one terminal window per
interrupted session, resumed in its original directory, told to carry on.

## How it works

- `bin/claude-session-registry` runs as a Claude Code `SessionStart` / `SessionEnd`
  hook. Each running session leaves a small file in `~/.claude/live-sessions/`
  (pid, boot id, session id, working directory) and removes it on a clean exit.
- `bin/claude-resume-crashed` reads that registry. Every entry whose process is
  gone, or that belongs to a previous boot, is a crashed session: it gets its own
  `gnome-terminal` window running `claude --resume <id>` plus a prompt asking the
  agent to summarise where it was and continue.
- `autostart/claude-resume-crashed.desktop` runs the script 15 seconds after the
  desktop login.

## Install

```bash
git clone https://github.com/FlexOr2/claude-revive ~/git/claude-revive
~/git/claude-revive/install.sh
```

`install.sh` symlinks the scripts into `~/.local/bin`, the desktop entry into
`~/.config/autostart`, and adds the two hooks to `~/.claude/settings.json`.
Sessions started after that point are tracked. Re-run it after a `git pull`; it
is idempotent.

## Use

```bash
claude-resume-crashed --list        # what would be reopened
claude-resume-crashed               # reopen and send the carry-on prompt
claude-resume-crashed --no-prompt   # reopen at the input line only
claude-resume-crashed --prompt "…"  # reopen with your own prompt
```

Nothing has to be typed after a crash; the autostart entry does the same as the
plain invocation.

## Limits

- Closing a terminal window with the mouse while Claude runs does not fire
  `SessionEnd`, so that session stays in the registry and is reopened on the
  next login. Exit with `Ctrl+C` or `/exit` instead.
- Terminal is `gnome-terminal`; other terminals need a one-line change in
  `claude-resume-crashed`.
- Requires Linux (`/proc`), `python3`, and Claude Code with hook support.
