# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Redaction policy for command lines and reports."""

import unittest

import tests.support  # noqa: F401
from scaffold.common.redaction import redact_argv, redact_report
from scaffold.common.results import Result


class RedactionTests(unittest.TestCase):
    def test_command_lines_hide_separated_joined_environment_and_url_secrets(self):
        self.assertEqual(
            redact_argv(["run", "--token=a", "--password", "b", "API_KEY=c", "https://u:d@host/x", "--name=e"]),
            ["run", "--token=***", "--password", "***", "API_KEY=***", "https://u:***@host/x", "--name=e"])

    def test_argument_lists_are_found_by_key_anywhere_in_a_report(self):
        report = {"data": {"argv": ["--token=a"], "steps": [{"package_arguments": ["--secret", "b"]}],
                           "files": ["--token=not-a-command-line"], "note": "see https://u:p@host/"}}
        self.assertEqual(redact_report(report), {"data": {
            "argv": ["--token=***"], "steps": [{"package_arguments": ["--secret", "***"]}],
            "files": ["--token=not-a-command-line"], "note": "see https://u:***@host/"}})

    def test_a_result_is_redacted_as_a_copy(self):
        result = Result(command="x", data={"argv": ["--token=a"]}, text="see https://u:p@host/")
        safe = result.redacted()
        self.assertEqual((safe.data, safe.text), ({"argv": ["--token=***"]}, "see https://u:***@host/"))
        self.assertEqual(result.data, {"argv": ["--token=a"]})


if __name__ == "__main__":
    unittest.main()
