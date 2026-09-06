"""What the installer does to a settings file, driven as the operator drives it.

Every run happens against a throwaway home under the test's own temporary
directory: the installer is given an explicit environment, so no run can fall
back to the operator's real `~/.claude/settings.json`.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent / "install.sh"
OPERATOR_SETTINGS = Path("~/.claude/settings.json").expanduser()
SESSION_HOOK_COMMANDS = {
    "SessionStart": "$HOME/.local/bin/claude-session-registry start $PPID",
    "SessionEnd": "$HOME/.local/bin/claude-session-registry end $PPID",
}


def operator_settings_signature() -> tuple[int, int, int] | None:
    """Enough of the operator's own file to notice a write, without reading it."""
    try:
        status = OPERATOR_SETTINGS.stat()
    except FileNotFoundError:
        return None
    return (status.st_ino, status.st_size, status.st_mtime_ns)


class InstallIntoAThrowawayHome(unittest.TestCase):
    """The installer runs as a command over a home that exists for one test."""

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.home = Path(temporary_directory.name)
        self.settings = self.home / ".claude" / "settings.json"

    def install(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(INSTALLER)],
            env={"HOME": str(self.home), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        )

    def install_successfully(self) -> None:
        completed = self.install()

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def write_settings(self, content: str) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(content, encoding="utf-8")

    def written_settings(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def registered_commands(self, event: str) -> list[str]:
        entries = self.written_settings()["hooks"][event]
        return [hook["command"] for entry in entries for hook in entry["hooks"]]

    def assert_both_session_hooks_are_registered(self) -> None:
        for event, command in SESSION_HOOK_COMMANDS.items():
            with self.subTest(event=event):
                self.assertIn(command, self.registered_commands(event))

    def test_a_missing_settings_file_is_created_with_both_session_hooks(self) -> None:
        self.install_successfully()

        self.assertEqual(list(self.written_settings()), ["hooks"])
        self.assert_both_session_hooks_are_registered()

    def test_settings_the_installer_did_not_write_survive_the_install(self) -> None:
        foreign_hook = {"hooks": [{"type": "command", "command": "notify-send hello"}]}
        self.write_settings(json.dumps({"model": "opus", "hooks": {"Notification": [foreign_hook]}}))

        self.install_successfully()

        self.assertEqual(self.written_settings()["model"], "opus")
        self.assertEqual(self.registered_commands("Notification"), ["notify-send hello"])
        self.assert_both_session_hooks_are_registered()

    def test_a_second_install_adds_no_duplicate_hook(self) -> None:
        self.install_successfully()
        self.install_successfully()

        for event, command in SESSION_HOOK_COMMANDS.items():
            with self.subTest(event=event):
                self.assertEqual(self.registered_commands(event), [command])

    def test_a_malformed_settings_file_is_refused_and_left_on_disk_as_it_was(self) -> None:
        malformed = '{"model": "opus"'
        self.write_settings(malformed)

        completed = self.install()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), malformed)
        self.assertEqual([path.name for path in self.settings.parent.iterdir()], ["settings.json"])

    def test_a_settings_file_kept_owner_only_is_still_owner_only_afterwards(self) -> None:
        """The replacement inherits the permissions of the file it replaces.

        Writing a fresh file and renaming it would otherwise hand the operator's
        settings — which carry an environment block — whatever the umask allows.
        """
        self.write_settings(json.dumps({"model": "opus"}))
        self.settings.chmod(0o600)

        self.install_successfully()

        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)

    def test_a_reader_holding_the_settings_file_open_sees_all_of_the_old_content(self) -> None:
        """The proof that the file is replaced by a rename instead of rewritten.

        A rename puts a different file at the path, so a descriptor opened
        before the install still reads the complete original content and the
        path carries a new inode afterwards. An in-place rewrite truncates the
        very file that descriptor points at, and both assertions catch it.
        """
        original = json.dumps({"model": "opus"}, indent=2) + "\n"
        self.write_settings(original)
        inode_before_the_install = self.settings.stat().st_ino

        with self.settings.open(encoding="utf-8") as reader_open_across_the_install:
            self.install_successfully()
            content_seen_by_that_reader = reader_open_across_the_install.read()

        self.assertEqual(content_seen_by_that_reader, original)
        self.assertNotEqual(self.settings.stat().st_ino, inode_before_the_install)
        self.assertEqual([path.name for path in self.settings.parent.iterdir()], ["settings.json"])

    def test_it_leaves_the_operators_own_settings_file_untouched(self) -> None:
        signature_before_the_install = operator_settings_signature()

        self.install_successfully()

        self.assertEqual(operator_settings_signature(), signature_before_the_install)


if __name__ == "__main__":
    unittest.main()
