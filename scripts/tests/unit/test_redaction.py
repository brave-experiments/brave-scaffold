# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Redaction policy for command lines and reports."""

import unittest
from unittest import mock

import tests.support  # noqa: F401
from scaffold.common.procs import _StreamOutput
from scaffold.common.redaction import Arguments, redact_argv, redact_report, redact_url_credentials
from scaffold.common.results import Result


class RedactionTests(unittest.TestCase):
    def test_command_lines_hide_separated_joined_environment_and_url_secrets(self):
        self.assertEqual(
            redact_argv(["run", "--token=a", "--password", "b", "API_KEY=c", "https://u:d@host/x", "--name=e"]),
            ["run", "--token=***", "--password", "***", "API_KEY=***", "https://u:***@host/x", "--name=e"])

    def test_names_that_only_start_with_auth_are_not_secrets(self):
        harmless = ["git", "log", "--author", "Chris", "--author=Chris", "GIT_AUTHOR_NAME=Chris",
                    "--authority", "x", "--authenticated"]
        self.assertEqual(redact_argv(harmless), harmless)
        self.assertEqual(
            redact_argv(["--auth", "a", "--authorization=b", "OAUTH_X=c", "--authToken=d", "AUTH_TOKEN=e"]),
            ["--auth", "***", "--authorization=***", "OAUTH_X=***", "--authToken=***", "AUTH_TOKEN=***"])

    def test_secret_headers_keep_their_name_and_lose_their_value(self):
        self.assertEqual(
            redact_argv(["curl", "-H", "Authorization: Bearer abc123", "-H", "X-Api-Key:k1", "-H", "Cookie: s=1",
                         "-H", "Accept: application/json"]),
            ["curl", "-H", "Authorization: ***", "-H", "X-Api-Key: ***", "-H", "Cookie: ***",
             "-H", "Accept: application/json"])

    def test_a_password_containing_at_signs_is_hidden_entirely(self):
        self.assertEqual(redact_argv(["git", "clone", "https://u:p@ss@host/path"]),
                         ["git", "clone", "https://u:***@host/path"])
        self.assertEqual(redact_url_credentials("see https://u:p@ss:w@host:8080/x"), "see https://u:***@host:8080/x")
        for untouched in ("ssh://git@host:22/repo", "https://host:8080/a@b", "git@github.com:org/repo.git"):
            self.assertEqual(redact_url_credentials(untouched), untouched)

    def test_output_scrubbing_ignores_values_too_short_to_be_secrets(self):
        scrub = _StreamOutput(mock.MagicMock(), ["tool", "--auth", "mac", "--token", "s3cretvalue"],
                              {"AUTH_X": "1", "API_KEY": "longapikey123"}, None, True)
        self.assertEqual(set(scrub.secrets), {"s3cretvalue", "longapikey123"})

    def test_headers_attached_to_an_option_are_redacted_without_hiding_the_next_argument(self):
        self.assertEqual(
            redact_argv(["curl", "--header=Authorization: Bearer abc123def456", "-HX-Api-Key: k1longvalue99",
                         "--proxy-header=Proxy-Authorization: Basic zzzzzz", "--header=Accept: application/json",
                         "-HAccept: text/plain", "https://example.invalid/x"]),
            ["curl", "--header=Authorization: ***", "-HX-Api-Key: ***", "--proxy-header=Proxy-Authorization: ***",
             "--header=Accept: application/json", "-HAccept: text/plain", "https://example.invalid/x"])

    def test_attached_header_secrets_are_scrubbed_from_child_output_too(self):
        saved = []
        log = mock.MagicMock()
        log.verbosity = "quiet"
        log.save.side_effect = saved.append
        output = _StreamOutput(log, ["curl", "-v", "--header=Authorization: Bearer abc123def456",
                                     "-HX-Api-Key: k1longvalue99", "--header=Accept: application/json"],
                               {}, None, True)
        output.write(None, "> Authorization: Bearer abc123def456\n> X-Api-Key: k1longvalue99\n"
                           "> Accept: application/json\n")
        text = "".join(saved)
        self.assertNotIn("abc123def456", text)
        self.assertNotIn("k1longvalue99", text)
        self.assertIn("Accept: application/json", text)

    def test_header_secrets_are_scrubbed_from_child_output_too(self):
        saved = []
        log = mock.MagicMock()
        log.verbosity = "quiet"
        log.save.side_effect = saved.append
        output = _StreamOutput(log, ["curl", "-v", "-H", "Authorization: Bearer abc123def456",
                                     "-H", "X-Api-Key:k1longvalue99", "-H", "Accept: application/json"],
                               {}, None, True)
        output.write(None, "> Authorization: Bearer abc123def456\n> X-Api-Key: k1longvalue99\n"
                           "> Accept: application/json\ntoken abc123def456 was echoed\n")
        text = "".join(saved)
        self.assertNotIn("abc123def456", text)
        self.assertNotIn("k1longvalue99", text)
        self.assertIn("> Authorization: ***", text)
        self.assertIn("Accept: application/json", text, "ordinary headers are left alone")

    def test_argument_lists_are_found_by_key_anywhere_in_a_report(self):
        report = {"data": {"argv": ["--token=a"], "steps": [{"package_arguments": ["--secret", "b"]}],
                           "files": ["--token=not-a-command-line"], "note": "see https://u:p@host/"}}
        self.assertEqual(redact_report(report), {"data": {
            "argv": ["--token=***"], "steps": [{"package_arguments": ["--secret", "***"]}],
            "files": ["--token=not-a-command-line"], "note": "see https://u:***@host/"}})

    def test_arguments_are_redacted_whatever_field_holds_them(self):
        report = {"error": {"details": {"any_field_name": Arguments(["--token", "a", "--name=b"])}}}
        self.assertEqual(redact_report(report), {"error": {"details": {
            "any_field_name": ["--token", "***", "--name=b"]}}})

    def test_a_result_is_redacted_as_a_copy(self):
        result = Result(command="x", data={"argv": ["--token=a"]}, text="see https://u:p@host/")
        safe = result.redacted()
        self.assertEqual((safe.data, safe.text), ({"argv": ["--token=***"]}, "see https://u:***@host/"))
        self.assertEqual(result.data, {"argv": ["--token=a"]})


if __name__ == "__main__":
    unittest.main()
