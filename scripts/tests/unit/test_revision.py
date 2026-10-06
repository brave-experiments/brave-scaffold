# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Scaffold identity remains independent of the selected browser checkout."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tests.support
from scaffold.common import revision
from scaffold.common.procs import CommandLog
from scaffold.brave import bdev
from scaffold.brave.records import Operation


class RevisionTests(unittest.TestCase):
    def test_git_identity_dirty_state_and_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()
            git("init", "-q")
            (root / "source").write_text("original")
            git("add", "source")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                "-c", "core.hooksPath=/dev/null", "commit", "--no-gpg-sign", "-qm", "Initial")
            sha = git("rev-parse", "HEAD")
            with patch.object(revision, "scaffold_root", return_value=root), patch.dict(
                    os.environ, {"GIT_DIR": "/missing/browser/.git", "GIT_WORK_TREE": "/missing/browser"}):
                clean = revision.read_revision()
                self.assertEqual(clean["sha"], sha)
                self.assertFalse(clean["dirty"])
                output = io.StringIO()
                with patch("scaffold.brave.app.config_module.load_config", side_effect=AssertionError("config read")):
                    self.assertEqual(bdev.main(["--version"], stdout=output), 0)
                self.assertEqual(output.getvalue(), "brave-scaffold %s (%s)\n" %
                                 (sha[:12], clean["commit_date"][:10]))
                (root / "source").write_text("changed")
                dirty = revision.read_revision()
                self.assertTrue(dirty["dirty"])
                self.assertTrue(revision.format_revision(dirty).endswith(" dirty"))
                log = CommandLog()
                log.open(root)
                log.close()
                self.assertEqual(json.loads(Path(log.path).read_text().splitlines()[0].split(": ", 1)[1]), dirty)
                operation = Operation("build", SimpleNamespace(core=root, src=root), {}, root=root)
                record = json.loads(operation.path.read_text())
                self.assertEqual(record["scaffold_revision"], dirty)
                self.assertNotIn("cli_version", record)
                (root / "source").write_text("original")
                self.assertTrue(revision.read_revision()["dirty"])  # Untracked files also count.
                unpacked = root / "unpacked"
                unpacked.mkdir()
                with patch.object(revision, "scaffold_root", return_value=unpacked):
                    self.assertIsNone(revision.read_revision()["sha"])

    def test_missing_git_and_failed_probes_are_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            for error in (FileNotFoundError(), subprocess.TimeoutExpired("git", 2),
                          subprocess.CalledProcessError(1, "git")):
                with self.subTest(error=type(error).__name__), patch.object(
                        revision, "scaffold_root", return_value=root), patch.object(
                        revision.subprocess, "run", side_effect=error):
                    self.assertEqual(revision.read_revision(), {"sha": None, "commit_date": None, "dirty": None})
                    output = io.StringIO()
                    self.assertEqual(bdev.main(["--version"], stdout=output), 0)
                    self.assertEqual(output.getvalue(), "brave-scaffold unknown\n")
