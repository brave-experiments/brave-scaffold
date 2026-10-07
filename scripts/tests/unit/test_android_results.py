# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Counting outcomes from a Chromium JSON results file."""

import json
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import android_tests


class SummarizeResultsTests(unittest.TestCase):
    def summarize(self, tests):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text(json.dumps({"per_iteration_data": [tests]}))
            summary = android_tests.summarize_results(path)
        return {key: summary[key] for key in ("passed", "failed", "skipped", "ran")}

    def test_every_skip_spelling_is_skipped_and_not_failed(self):
        counts = self.summarize({
            "a.B#ok": [{"status": "SUCCESS"}], "a.B#skip": [{"status": "SKIP"}],
            "a.B#skipped": [{"status": "SKIPPED"}], "a.B#notrun": [{"status": "NOTRUN"}]})
        self.assertEqual(counts, {"passed": 1, "failed": 0, "skipped": 3, "ran": 1})

    def test_failures_crashes_and_timeouts_still_fail(self):
        counts = self.summarize({
            "a.B#fail": [{"status": "FAILURE"}], "a.B#crash": [{"status": "CRASH"}],
            "a.B#slow": [{"status": "TIMEOUT"}], "a.B#flaky": [{"status": "FAILURE"}, {"status": "SUCCESS"}]})
        self.assertEqual(counts, {"passed": 1, "failed": 3, "skipped": 0, "ran": 4})


if __name__ == "__main__":
    unittest.main()
