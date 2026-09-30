# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Local-work detection covers the index, the worktree, and untracked files."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import gitstate
from scaffold.common.results import ScaffoldError

GIT = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


class ChangedPathsTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="scaffold-gitstate-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.repo, ignore_errors=True))
        self.git("init", "-q")
        for name in ("clean.cc", "modified.cc", "staged.cc", "deleted.cc", "staged_deleted.cc", "moved_from.cc"):
            (self.repo / name).write_text(name + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args):
        subprocess.run([*GIT, "-C", str(self.repo), *args], check=True, capture_output=True)

    def test_every_kind_of_local_work_is_reported_and_clean_files_are_not(self):
        (self.repo / "modified.cc").write_text("edited\n")
        (self.repo / "staged.cc").write_text("staged\n")
        self.git("add", "staged.cc")
        (self.repo / "deleted.cc").unlink()
        self.git("rm", "-q", "staged_deleted.cc")
        (self.repo / "untracked.cc").write_text("new\n")
        self.git("mv", "moved_from.cc", "moved_to.cc")
        names = ["clean.cc", "modified.cc", "staged.cc", "deleted.cc", "staged_deleted.cc", "untracked.cc",
                 "moved_from.cc", "moved_to.cc", "absent.cc"]
        self.assertEqual(gitstate.changed_paths(self.repo, names),
                         {"modified.cc", "staged.cc", "deleted.cc", "staged_deleted.cc", "untracked.cc",
                          "moved_from.cc", "moved_to.cc"})

    def test_a_directory_that_is_not_a_repository_is_an_error_not_a_clean_answer(self):
        with self.assertRaises(ScaffoldError) as caught:
            gitstate.changed_paths(tempfile.gettempdir() + "/definitely-not-a-repo-%d" % id(self), ["x"])
        self.assertEqual(caught.exception.code, "PREPARATION_CONFLICT")
        outside = Path(tempfile.mkdtemp(prefix="scaffold-nogit-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        with self.assertRaises(ScaffoldError):
            gitstate.changed_paths(outside, ["x"])

    def test_paths_are_literal_not_patterns(self):
        (self.repo / "star*.cc").write_text("x\n")
        self.assertEqual(gitstate.changed_paths(self.repo, ["*.cc"]), set())
        self.assertEqual(gitstate.changed_paths(self.repo, ["star*.cc"]), {"star*.cc"})


if __name__ == "__main__":
    unittest.main()
