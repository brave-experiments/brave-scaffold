# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which phase failures let `bcore test` carry on with the remaining suites."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import branch_tests, cmd_test
from scaffold.common.cli import Parsed
from scaffold.common.results import ScaffoldError

PHASES = [branch_tests.Phase("mac", "brave_unit_tests", ["A.*"], ["a_unittest.cc"]),
          branch_tests.Phase("mac", "brave_browser_tests", ["B.*"], ["b_browsertest.cc"])]


class PhaseFailureTests(unittest.TestCase):
    def run_phases(self, first_error):
        discovery = branch_tests.Discovery("base-ref", "both", 2, [], list(PHASES), [])
        ctx = mock.MagicMock()
        ctx.parsed = Parsed(values={"base": "base-ref"}, forwarded=[])
        selection = (SimpleNamespace(core=Path("/checkout")),
                     SimpleNamespace(target="mac", sources={"target": "default"}))
        suites = []

        def run_suite(phase):
            suites.append(phase.suite)
            if len(suites) == 1:
                raise first_error
            return mock.MagicMock(error=None, data={}, warnings=[], operation_id="op")

        with mock.patch.object(cmd_test.cmd_build, "select_build", return_value=selection), \
                mock.patch.object(cmd_test.branch_tests, "discover", return_value=discovery), \
                mock.patch.object(cmd_test, "phase_context", side_effect=lambda ctx, phase, device: phase), \
                mock.patch.object(cmd_test.cmd_build, "cmd_test", side_effect=run_suite):
            with self.assertRaises(ScaffoldError) as caught:
                cmd_test.cmd_test_discovered(ctx)
        return suites, caught.exception

    def test_a_failing_test_run_does_not_stop_the_remaining_suites(self):
        error = ScaffoldError("CHILD_FAILED", "The package test command exited with status 2.",
                              details={"argv": ["npm"]}, child_exit_code=2)
        suites, raised = self.run_phases(error)
        self.assertEqual(suites, ["brave_unit_tests", "brave_browser_tests"])
        self.assertEqual(raised.code, "CHILD_FAILED")
        self.assertIn("1 of 2 test phase(s) failed", raised.message)

    def test_a_failed_patch_application_stops_the_remaining_suites(self):
        error = ScaffoldError("CHILD_FAILED", "Applying Core patches failed (exit 1).",
                              details={"argv": ["npm"], "phase": "patches"}, child_exit_code=1)
        suites, raised = self.run_phases(error)
        self.assertEqual(suites, ["brave_unit_tests"])
        self.assertEqual(raised.code, "CHILD_FAILED")
        self.assertEqual(raised.details["not_run"], ["brave_unit_tests", "brave_browser_tests"])
        self.assertIn("Test run incomplete", raised.message)


if __name__ == "__main__":
    unittest.main()
