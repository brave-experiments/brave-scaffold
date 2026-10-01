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

    def test_snapshot_keeps_index_and_worktree_changes_with_two_git_calls(self):
        from scaffold.common.procs import CommandLog
        (self.repo / "modified.cc").write_text("modified\n")
        self.git("mv", "moved_from.cc", "moved_to.cc")
        log = CommandLog(enabled=False)
        head, paths = gitstate.tracked_snapshot(self.repo, log)
        self.assertEqual(head, gitstate.head_commit(self.repo))
        self.assertEqual(paths, {"modified.cc", "moved_from.cc", "moved_to.cc"})
        self.assertEqual(len(log.records), 2)
        nested = self.repo / "broken-dependency"
        nested.mkdir()
        with self.assertRaises(ScaffoldError):
            gitstate.tracked_snapshot(nested)

    def test_paths_are_literal_not_patterns(self):
        (self.repo / "star*.cc").write_text("x\n")
        self.assertEqual(gitstate.changed_paths(self.repo, ["*.cc"]), set())
        self.assertEqual(gitstate.changed_paths(self.repo, ["star*.cc"]), {"star*.cc"})

    def test_index_changes_are_separate_from_worktree_and_untracked_changes(self):
        (self.repo / "staged.cc").write_text("index version\n")
        self.git("add", "staged.cc")
        (self.repo / "staged.cc").write_text("working version\n")
        (self.repo / "modified.cc").write_text("unstaged\n")
        (self.repo / "new.cc").write_text("untracked\n")
        self.git("rm", "-q", "staged_deleted.cc")
        self.git("mv", "moved_from.cc", "moved_to.cc")
        self.assertEqual(gitstate.staged_paths(self.repo),
                         {"staged.cc", "staged_deleted.cc", "moved_from.cc", "moved_to.cc"})
        self.assertEqual(gitstate.staged_paths(self.repo, ["modified.cc", "staged.cc", "new.cc"]), {"staged.cc"})
        self.assertEqual(gitstate.staged_paths(self.repo, []), set())


class HeadTests(unittest.TestCase):
    """One reader answers 'what is checked out' for every supported repository layout."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="scaffold-heads-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def repo(self, name="repo"):
        path = self.root / name
        path.mkdir()
        subprocess.run([*GIT, "-C", str(path), "init", "-q", "-b", "main"], check=True, capture_output=True)
        (path / "a.txt").write_text("a\n")
        subprocess.run([*GIT, "-C", str(path), "add", "-A"], check=True, capture_output=True)
        subprocess.run([*GIT, "-C", str(path), "commit", "-q", "-m", "one"], check=True, capture_output=True)
        return path

    def expected(self, path):
        return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_ordinary_packed_and_detached_repositories(self):
        path = self.repo()
        self.assertEqual(gitstate.head_commit(path), self.expected(path))
        self.assertEqual(gitstate.head_description(path), {"branch": "main"})
        subprocess.run(["git", "-C", str(path), "pack-refs", "--all"], check=True, capture_output=True)
        self.assertEqual(gitstate.head_commit(path), self.expected(path))
        subprocess.run(["git", "-C", str(path), "checkout", "-q", "--detach"], check=True, capture_output=True)
        self.assertEqual(gitstate.head_description(path), {"detached": self.expected(path)})

    def test_a_git_file_with_a_relative_gitdir_is_anchored_to_its_own_repository(self):
        path = self.repo()
        (self.root / "elsewhere").mkdir()
        (path / ".git").rename(self.root / "elsewhere" / "real.git")
        (path / ".git").write_text("gitdir: ../elsewhere/real.git\n")
        cwd = Path.cwd()
        self.addCleanup(lambda: __import__("os").chdir(cwd))
        __import__("os").chdir(tempfile.gettempdir())
        real = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertRegex(real, "^[0-9a-f]{40}$")
        self.assertEqual(gitstate.head_commit(path), real)
        from scaffold.brave import freshness
        self.assertEqual(freshness.resolve_head(path), real)

    def test_missing_history_and_non_repositories_have_no_head(self):
        empty = self.root / "empty"
        empty.mkdir()
        subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True, capture_output=True)
        self.assertIsNone(gitstate.head_commit(empty))
        self.assertIsNone(gitstate.head_commit(self.root / "not-a-repo"))
        self.assertIsNone(gitstate.head_description(self.root / "not-a-repo"))


if __name__ == "__main__":
    unittest.main()
