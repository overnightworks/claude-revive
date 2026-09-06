"""What the root layout gate refuses, driven as CI drives it: a python over the script."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_root_layout

ALLOWLIST = check_root_layout.REPOSITORY_ALLOWLIST
GATE = Path(check_root_layout.__file__).resolve()
# git's own configuration is pinned to the null device for the probe repository
# and for the gate run over it: the gate asks git which untracked files are
# ignored, so a machine-wide ignore matching a probe file would otherwise turn
# a red path green on that machine only.
ISOLATED_GIT = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


class GateOverARepository(unittest.TestCase):
    """The gate runs as a command over a temporary repository it is happy with."""

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.repository = Path(temporary_directory.name)
        (self.repository / "scripts").mkdir()
        shutil.copy(GATE, self.repository / "scripts" / GATE.name)
        (self.repository / "README.md").write_text("a repository the gate is happy with\n", encoding="utf-8")
        (self.repository / ".gitignore").write_text("*.log\n", encoding="utf-8")
        self.git("init", "--quiet")
        self.commit_the_tree()

    def git(self, *arguments: str) -> None:
        # An identity is pinned for the probe repository so committing works on
        # a machine and a runner that configure none.
        identity = ("-c", "user.name=layout gate test", "-c", "user.email=gate@example.invalid")
        subprocess.run(
            ["git", *identity, *arguments],
            cwd=self.repository,
            env=ISOLATED_GIT,
            check=True,
            capture_output=True,
        )

    def commit_the_tree(self) -> None:
        """Make the probe tree tracked, as a checked-out repository is."""
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", "the probe tree")

    def run_gate(self, working_directory: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.repository / "scripts" / GATE.name)],
            cwd=working_directory,
            env=ISOLATED_GIT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_it_passes_on_an_allowlisted_tree(self) -> None:
        completed = self.run_gate(self.repository)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("passed", completed.stdout)

    def test_it_leaves_a_root_file_git_ignores_alone(self) -> None:
        (self.repository / "resume.log").write_text("a local artefact\n", encoding="utf-8")

        completed = self.run_gate(self.repository)

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_it_names_a_committed_stray_root_file_and_fails(self) -> None:
        (self.repository / "NOTES.md").write_text("a stray somebody committed\n", encoding="utf-8")
        self.commit_the_tree()

        completed = self.run_gate(self.repository)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("NOTES.md", completed.stderr)

    def test_it_names_an_uncommitted_stray_root_file_and_fails_from_any_directory(self) -> None:
        (self.repository / "NOTES.md").write_text("a stray at the root\n", encoding="utf-8")

        for working_directory in (self.repository, self.repository / "scripts"):
            with self.subTest(working_directory=working_directory.name):
                completed = self.run_gate(working_directory)

                self.assertEqual(completed.returncode, 1)
                self.assertIn("NOTES.md", completed.stderr)


class RootLayoutProblems(unittest.TestCase):
    """Which listing entries the allowlist refuses, and with which sentence."""

    def test_an_allowlisted_listing_carries_no_problem(self) -> None:
        listing = [
            ".gitignore",
            "AGENTS.md",
            "LICENSE",
            "README.md",
            "sonar-project.properties",
            ".github/workflows/ci.yml",
            "autostart/claude-resume-crashed.desktop",
            "bin/claude-resume-crashed",
            "scripts/check_root_layout.py",
        ]

        self.assertEqual(check_root_layout.root_layout_problems(listing, ALLOWLIST), ())

    def test_a_stray_root_file_is_named_with_the_sentence_where_it_belongs(self) -> None:
        strays_and_homes = [
            ("NOTES.md", check_root_layout.HOME_BY_SUFFIX[".md"]),
            ("install.sh", check_root_layout.HOME_BY_SUFFIX[".sh"]),
            ("check_root_layout.py", check_root_layout.HOME_BY_SUFFIX[".py"]),
            ("claude-resume-crashed.desktop", check_root_layout.HOME_BY_SUFFIX[".desktop"]),
        ]

        for stray, home in strays_and_homes:
            with self.subTest(stray=stray):
                problems = check_root_layout.root_layout_problems(["README.md", stray], ALLOWLIST)

                self.assertEqual(problems, (f"{stray}: {home}",))

    def test_a_stray_command_is_sent_to_the_directory_that_owns_commands(self) -> None:
        """This repository's commands carry no suffix, so the default sentence names bin/."""
        (problem,) = check_root_layout.root_layout_problems(["README.md", "claude-resume-crashed"], ALLOWLIST)

        self.assertTrue(problem.startswith("claude-resume-crashed: "), problem)
        self.assertIn("bin/", problem)

    def test_a_stray_root_directory_is_named_with_the_sentence_where_it_belongs(self) -> None:
        problems = check_root_layout.root_layout_problems(["README.md", "tooling/helper.py"], ALLOWLIST)

        self.assertEqual(problems, (f"tooling/: {check_root_layout.DIRECTORY_HOME}",))


if __name__ == "__main__":
    unittest.main()
