# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The multi-device test report is written by a child process that may die mid-write."""

import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import cmd_build


class DeviceReportTests(unittest.TestCase):
    def read(self, text=None):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            if text is not None:
                report.write_text(text)
            return cmd_build.read_device_runs(report)

    def test_a_complete_report_gives_its_runs(self):
        runs = [{"device": "emulator-5554", "status": "finished", "exit": 0}]
        self.assertEqual(self.read('{"runs": [{"device": "emulator-5554", "status": "finished", "exit": 0}]}'), runs)

    def test_a_missing_partial_or_malformed_report_gives_no_runs(self):
        for text in (None, "", '{"runs": [{"device":', "not json", "[]", "null", "{}", '{"runs": null}',
                     '{"runs": {"a": 1}}'):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), [])


if __name__ == "__main__":
    unittest.main()
