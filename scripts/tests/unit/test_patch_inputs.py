# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch input inspection never treats an unanswered Git question as a clean tree."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import patches
from scaffold.common.results import ScaffoldError

IDENTITY = SimpleNamespace(core=Path("/checkout/src/brave"))


def answer(returncode=0, stdout="", truncated=False, timed_out=False):
    return SimpleNamespace(returncode=returncode, stdout=stdout, truncated=truncated, timed_out=timed_out)


def fake_git(status):
    def run(identity, repo, args, log):
        return answer(stdout="tree-id\n") if args[0] == "rev-parse" else status
    return run


class PatchInputsTests(unittest.TestCase):
    def inspect(self, status):
        with mock.patch.object(patches, "_git", fake_git(status)):
            return patches.patch_inputs(IDENTITY)

    def test_a_clean_and_a_dirty_answer_are_reported_as_given(self):
        trees, dirty = self.inspect(answer(stdout=""))
        self.assertEqual((trees, dirty), ({"patches": "tree-id", "rewrite": "tree-id"}, []))
        self.assertEqual(self.inspect(answer(stdout=" M patches/a.patch\n"))[1], [" M patches/a.patch"])

    def test_a_failed_timed_out_or_truncated_status_stops_instead_of_looking_clean(self):
        cases = {"failed": answer(returncode=128), "timed out": answer(returncode=124, timed_out=True),
                 "truncated": answer(truncated=True)}
        for name, status in cases.items():
            with self.subTest(name):
                with self.assertRaises(ScaffoldError) as caught:
                    self.inspect(status)
                self.assertEqual(caught.exception.code, "PREPARATION_CONFLICT")
                self.assertIn("could not be inspected", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
