# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Freshness evidence: which inputs are compared and when the answer is unknown."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import freshness


class State:
    needs_revalidation = False

    def last_attempt(self):
        return None


CURRENT = {"core_head": "a", "chromium_head": "b", "core_worktree": "c", "patched_files": "d", "env_file": "e",
           "chromium_worktree": "f"}


class TrackedChangesTests(unittest.TestCase):
    """The Chromium tree's fingerprint reflects edits, not file timestamps."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"]):
            self.git(*args)
        (self.repo / "a.txt").write_text("hello\n")
        (self.repo / "b.txt").write_text("world\n")
        self.git("add", ".")
        self.git("commit", "-qm", "init")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def state(self):
        return freshness.tracked_changes_state(self.repo)

    def test_touching_a_file_without_changing_it_does_not_change_the_fingerprint(self):
        before = self.state()
        os.utime(self.repo / "a.txt", ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(self.state(), before)
        self.git("status", "--porcelain")  # an unrelated command refreshes the index
        self.assertEqual(self.state(), before)

    def test_a_same_length_edit_with_a_restored_timestamp_still_changes_every_fingerprint(self):
        (self.repo / "a.txt").write_text("edited AAAA\n")
        (self.repo / "untracked.txt").write_text("new AAAA\n")
        stamps = {name: os.stat(self.repo / name).st_mtime_ns for name in ("a.txt", "untracked.txt")}
        tracked, worktree = self.state(), freshness.worktree_state(self.repo)
        (self.repo / "a.txt").write_text("edited BBBB\n")
        (self.repo / "untracked.txt").write_text("new BBBB\n")
        for name, ns in stamps.items():
            os.utime(self.repo / name, ns=(ns, ns))
        self.assertNotEqual(self.state(), tracked)
        self.assertNotEqual(freshness.worktree_state(self.repo), worktree)

    def test_an_edit_changes_the_fingerprint_and_reverting_it_restores_it(self):
        before = self.state()
        (self.repo / "a.txt").write_text("hello, edited\n")
        edited = self.state()
        self.assertNotEqual(edited, before)
        (self.repo / "a.txt").write_text("hello\n")
        self.git("status", "--porcelain")
        self.assertEqual(self.state(), before)

    def test_staged_changes_count_and_a_missing_repository_is_unknown(self):
        before = self.state()
        (self.repo / "b.txt").write_text("staged\n")
        self.git("add", "b.txt")
        self.assertNotEqual(self.state(), before)
        self.assertIsNone(freshness.tracked_changes_state(self.repo / "nowhere"))


class FileSignatureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def test_the_signature_follows_the_content_not_the_size_or_timestamp(self):
        path = self.dir / "f.txt"
        path.write_text("AAAA\n")
        ns = os.stat(path).st_mtime_ns
        before = freshness._file_signature(path)
        path.write_text("BBBB\n")
        os.utime(path, ns=(ns, ns))
        self.assertNotEqual(freshness._file_signature(path), before)
        path.write_text("AAAA\n")
        os.utime(path, ns=(ns + 5_000_000_000, ns + 5_000_000_000))
        self.assertEqual(freshness._file_signature(path), before, "a touched but identical file is unchanged")

    def test_missing_files_directories_and_links_have_their_own_signatures(self):
        (self.dir / "d").mkdir()
        (self.dir / "t1.txt").write_text("one")
        (self.dir / "t2.txt").write_text("two")
        (self.dir / "link").symlink_to("t1.txt")
        first = freshness._file_signature(self.dir / "link")
        (self.dir / "link").unlink()
        (self.dir / "link").symlink_to("t2.txt")
        signatures = {freshness._file_signature(self.dir / "nowhere"), freshness._file_signature(self.dir / "d"),
                      first, freshness._file_signature(self.dir / "link"),
                      freshness._file_signature(self.dir / "t1.txt")}
        self.assertEqual(len(signatures), 5, signatures)
        self.assertEqual(freshness._file_signature(self.dir / "nowhere"), "missing")

    def test_a_large_file_is_hashed_in_full(self):
        path = self.dir / "big.bin"
        path.write_bytes(b"x" * (3 << 20))
        before = freshness._file_signature(path)
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(bytes(data))
        self.assertNotEqual(freshness._file_signature(path), before)


class AssessFormatTests(unittest.TestCase):
    def test_a_record_made_before_content_hashing_is_unknown_not_stale(self):
        result = freshness.assess(dict(CURRENT), {**CURRENT, "signature_format": "content-sha256-v1",
                                                  "patched_files": "different"}, State())
        self.assertEqual(result["status"], "unknown")
        self.assertIn("signature_format", " ".join(result["evidence"]))

    def test_the_fingerprint_names_its_signature_format(self):
        self.assertIn("signature_format", freshness.TRACKED)


class AssessTests(unittest.TestCase):
    def test_a_match_on_every_input_is_current_and_names_what_is_not_tracked(self):
        result = freshness.assess(dict(CURRENT), dict(CURRENT), State())
        self.assertEqual(result["status"], "current")
        self.assertIn("not tracked", " ".join(result["evidence"]))

    def test_any_changed_input_is_stale(self):
        for key in CURRENT:
            with self.subTest(key=key):
                result = freshness.assess(dict(CURRENT), {**CURRENT, key: "changed"}, State())
                self.assertEqual(result["status"], "stale")
                self.assertIn(key, " ".join(result["evidence"]))

    def test_an_input_that_could_not_be_computed_makes_the_answer_unknown(self):
        result = freshness.assess(dict(CURRENT), {**CURRENT, "chromium_worktree": None}, State())
        self.assertEqual(result["status"], "unknown")

    def test_a_record_without_evidence_the_current_check_needs_is_unknown_not_current(self):
        old = {key: value for key, value in CURRENT.items() if key != "chromium_worktree"}
        result = freshness.assess(old, dict(CURRENT), State())
        self.assertEqual(result["status"], "unknown")
        self.assertIn("chromium_worktree", " ".join(result["evidence"]))

    def test_dependency_evidence_is_compared_and_missing_evidence_is_unknown(self):
        recorded = {**CURRENT, "dependency_heads": "h", "dependency_changes": "c"}
        self.assertEqual(freshness.assess(dict(recorded), dict(recorded), State())["status"], "current")
        for key in ("dependency_heads", "dependency_changes"):
            with self.subTest(key=key):
                self.assertEqual(freshness.assess(dict(recorded), {**recorded, key: "moved"}, State())["status"],
                                 "stale")
                self.assertEqual(freshness.assess(dict(recorded), {**recorded, key: None}, State())["status"],
                                 "unknown")
        self.assertEqual(freshness.assess(dict(CURRENT), dict(recorded), State())["status"], "unknown",
                         "a record from before dependencies were compared says nothing about them")

    def test_inputs_that_do_not_apply_are_not_compared(self):
        without = {key: value for key, value in CURRENT.items() if key != "chromium_worktree"}
        self.assertEqual(freshness.assess(dict(without), dict(without), State())["status"], "current")


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.core = Path(tempfile.mkdtemp(prefix="scaffold-env-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.core, ignore_errors=True))

    def write(self, name, text):
        path = self.core / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def fingerprint(self):
        return freshness.env_files_fingerprint(self.core)

    def test_absent_file(self):
        self.assertEqual(self.fingerprint(), "absent")

    def test_included_files_are_part_of_the_fingerprint_at_any_depth(self):
        self.write(".env", "A=1\ninclude_env=env/one.env # comment\n")
        self.write("env/one.env", "B=2\ninclude_env=two.env\n")
        self.write("env/two.env", "C=3\n")
        first = self.fingerprint()
        self.write("env/two.env", "C=4\n")
        second = self.fingerprint()
        self.write("env/one.env", "B=2\ninclude_env=two.env\nD=5\n")
        self.assertEqual(len({first, second, self.fingerprint()}), 3)

    def test_a_missing_include_and_a_cycle_are_handled(self):
        self.write(".env", "include_env=gone.env\n")
        missing = self.fingerprint()
        self.write("gone.env", "include_env=.env\n")
        self.assertNotEqual(self.fingerprint(), missing)


if __name__ == "__main__":
    unittest.main()
