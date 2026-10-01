# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Doctor's text report uses scannable status markers; structured statuses and exit codes are unchanged."""

import io
import json
import subprocess
import unittest

import tests.support  # noqa: F401
from scaffold.brave import doctor
from scaffold.common.checks import BLOCKER, NOT_CHECKED, PASS, UNSUPPORTED, WARNING, make_check
from tests.support import SCRIPTS, Sandbox
from scaffold.common.results import Result, emit


class RenderTests(unittest.TestCase):
    def report(self):
        checks = [make_check("fine", PASS, "All good.", "mac"),
                  make_check("broken", BLOCKER, "Needs work.", "mac"),
                  make_check("careful", WARNING, "Look at this.", "mac", required=False),
                  make_check("never", UNSUPPORTED, "Not possible here.", "mac", required=False),
                  make_check("later", NOT_CHECKED, "No checkout selected.", "mac")]
        return doctor.render_text(["mac"], checks)

    def test_a_mixed_report_marks_each_status_and_explains_the_markers(self):
        lines = self.report().splitlines()
        marked = {line.split()[1].rstrip(":"): line.split()[0] for line in lines[:-1] if line.startswith(("✅", "❌", "⚠️", "🚫", "❔"))}
        self.assertEqual(marked, {"fine": "✅", "broken": "❌", "careful": "⚠️", "never": "🚫", "later": "❔"})
        legend = lines[-1]
        for marker, word in (("✅", "pass"), ("❌", "blocker"), ("⚠️", "warning"), ("🚫", "unsupported"),
                             ("❔", "not checked")):
            self.assertIn("%s %s" % (marker, word), legend)

    def test_duplicate_checks_keep_required_failure_and_all_scopes(self):
        checks = doctor.merge_checks([
            make_check("rbe-env", WARNING, "Missing RBE config", "mac", required=False),
            make_check("rbe-env", BLOCKER, "Missing RBE config", "rbe"),
            make_check("rbe-env", WARNING, "Missing RBE config", "android", required=False)])
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].required)
        self.assertEqual(checks[0].status, BLOCKER)
        self.assertEqual(checks[0].scopes, ("mac", "rbe", "android"))

    def test_report_does_not_hide_unrelated_errors_or_log_warnings(self):
        result = Result(command="doctor", text="Doctor report", checks=[{}],
                        error={"code": "INTERNAL_ERROR", "message": "Unexpected failure"})
        result.add_warning("LOG_WRITE_FAILED", "Cannot finish the log")
        result.add_warning("CHECK_WARNING", "Already in the report")
        stderr = io.StringIO()
        emit(result, False, stdout=io.StringIO(), stderr=stderr)
        self.assertIn("INTERNAL_ERROR", stderr.getvalue())
        self.assertIn("LOG_WRITE_FAILED", stderr.getvalue())
        self.assertNotIn("CHECK_WARNING", stderr.getvalue())

    def test_details_stay_readable_next_to_the_marker(self):
        text = self.report()
        self.assertIn("❌  broken: Needs work.", text)
        self.assertIn("⚠️  careful (optional): Look at this.", text)
        self.assertNotIn("BLOCKER", text)


class CliTests(unittest.TestCase):
    def test_json_statuses_and_exit_codes_do_not_change(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        sandbox.write_config([])
        text = sandbox.bdev("--config", str(sandbox.config), "doctor", "mac")
        as_json = sandbox.bdev("--json", "--config", str(sandbox.config), "doctor", "mac")
        self.assertEqual(text.returncode, as_json.returncode)
        document = json.loads(as_json.stdout)
        self.assertEqual(document["error"]["code"], "READINESS_INCOMPLETE")
        statuses = {check["name"]: check["status"] for check in document["checks"]}
        self.assertEqual(statuses["checkout-selection"], "not_checked")
        self.assertIn("Checkout checks need a selected checkout", text.stdout)
        self.assertEqual(text.stdout.count("Next: bdev checkout list"), 1)
        self.assertNotIn("Warning [CHECK_", text.stderr)
        self.assertNotIn("Error [READINESS_", text.stderr)
        self.assertNotIn("NOT_CHECKED", text.stdout)


if __name__ == "__main__":
    unittest.main()
