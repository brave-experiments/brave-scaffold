# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""User-supplied support repository sources and refs never reach Git as options."""

import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import android_deps
from scaffold.common.procs import ProcessResult
from scaffold.common.results import ScaffoldError


class GitArgumentTests(unittest.TestCase):
    def test_a_source_or_ref_that_looks_like_an_option_is_refused_before_any_work(self):
        cases = (("--upload-pack=touch /tmp/pwned", None, "--source"), ("-c", None, "--source"),
                 (None, "--orphan", "--ref"), (None, "-f", "--ref"))
        for source, ref, option in cases:
            with self.subTest(source=source, ref=ref), \
                    mock.patch.object(android_deps, "run_capture") as run, \
                    mock.patch.object(android_deps, "_git") as git:
                with self.assertRaises(ScaffoldError) as caught:
                    android_deps.setup_working_copy(None, None, source, ref)
                self.assertEqual(caught.exception.code, "INVALID_INPUT")
                self.assertIn(option, caught.exception.message)
                run.assert_not_called()
                git.assert_not_called()

    def test_checkout_names_the_ref_as_a_revision_not_a_path(self):
        with mock.patch.object(android_deps, "run_capture", return_value=ProcessResult(returncode=0)) as run:
            android_deps._checkout(Path("/support"), "v155", None)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["v155", "--"])

    def test_clone_ends_option_parsing_before_the_source(self):
        argv = android_deps._clone_argv("https://example.invalid/support.git", Path("/shared"))
        self.assertEqual(argv, ["git", "clone", "--", "https://example.invalid/support.git", "/shared"])


if __name__ == "__main__":
    unittest.main()
