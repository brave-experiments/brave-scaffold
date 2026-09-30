# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Freshness evidence: which inputs are compared and when the answer is unknown."""

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
