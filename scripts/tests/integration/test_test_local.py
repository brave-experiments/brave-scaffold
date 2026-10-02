# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""`bdev test-local`: modified tests become suite phases that run through the existing test paths."""

import subprocess
from pathlib import Path

from tests.integration.test_android_tests import BRANCH, DEVICES_TWO, ONE_DEVICE, AndroidTestsTestCase

JAVA = "package org.example.app;\n\npublic class %s {}\n"


class TestLocalTests(AndroidTestsTestCase):
    def setUp(self):
        super().setUp()
        self.git("branch", "base-ref")

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                        "-C", str(self.core), *args], check=True, capture_output=True)

    def add_tests(self, *names):
        files = {"junit": "android/junit/src/org/example/app/BarUnitTest.java",
                 "java": "android/javatests/org/example/app/FooTest.java",
                 "unit": "browser/foo_unittest.cc", "native": "browser/extensions/android/n_unittest.cc"}
        for name in names:
            path = self.core / files[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(JAVA % path.stem if name in ("junit", "java") else "TEST_F(FooUnitTest, One) {}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "tests")

    def local(self, *args, devices=ONE_DEVICE, **env):
        return self.document("test-local", "--base", "base-ref", *args, env=self.env(FAKE_ADB_DEVICES=devices, **env))

    def test_runs_each_modified_suite_with_its_filter_in_order(self):
        self.on_test_branch()
        self.add_tests("java", "junit", "unit", "native")
        result, document = self.local("--device", "emulator-5554", devices=DEVICES_TWO)
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
        runs = [call["argv"][call["argv"].index("test") + 1:][:2] for call in self.runner_calls()]
        self.assertEqual(runs, [["brave_junit_tests", "--filter=org.example.app.BarUnitTest.*"],
                                ["brave_java_unit_tests", "--filter=FooTest.*"],
                                ["brave_unit_tests", "--filter=FooUnitTest.*"]])
        device_run = self.runner_calls()[1]["argv"]
        self.assertEqual(device_run[device_run.index("--device") + 1], "emulator-5554")
        self.assertEqual([p["status"] for p in document["data"]["phases"]], ["passed"] * 3)
        self.assertEqual([item["path"] for item in document["data"]["discovery"]["unmapped"]],
                         ["browser/extensions/android/n_unittest.cc"])
        self.assertEqual(result.stderr.count("browser/extensions/android/n_unittest.cc"), 1, "listed once, not repeated")
        self.assertIn("Not run (Android native tests are not available through bdev):", result.stderr)
        self.assertIn("--filter=FooTest.*", result.stderr, "the filters run are in the log")
        self.assertIn("Phase 2/3: android brave_java_unit_tests", result.stderr)

    def test_a_host_only_branch_needs_no_device(self):
        self.on_test_branch()
        self.add_tests("junit")
        result, document = self.local(devices="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.adb_calls(), [])

    def summary_run(self, **env):
        return self.sandbox.bdev("--config", self.config, "--checkout", "main", "test-local", "android",
                                 "--base", "base-ref", env=self.env(FAKE_ADB_DEVICES=ONE_DEVICE, **env))

    def test_final_summary_counts_each_suite_and_repeats_unmapped_files(self):
        self.on_test_branch()
        self.add_tests("junit", "java", "native")
        hook = Path(self.hook)
        hook.write_text(hook.read_text().replace(
            "    if targets and mode in tests:",
            "    if suite == 'brave_java_unit_tests':\n"
            "        del tests['pass']['a.B#two']\n"
            "    if targets and mode in tests:"))
        result = self.summary_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\n--------------------------------\nTest summary:\n", result.stdout)
        summary = result.stdout.split("Test summary:", 1)[1]
        for suite, count in (("brave_junit_tests", 2), ("brave_java_unit_tests", 1)):
            row = next(line for line in summary.splitlines() if suite in line)
            self.assertIn("✅", row)
            self.assertIn("%d run, %d passed, 0 failed, 1 skipped" % (count, count), row)
        self.assertIn("✅ All run tests passed.", summary)
        rows = summary.splitlines()
        for suite, expected_filter in (("brave_junit_tests", "org.example.app.BarUnitTest.*"),
                                       ("brave_java_unit_tests", "FooTest.*")):
            index = next(i for i, row in enumerate(rows) if suite in row)
            self.assertEqual(rows[index + 1], "     --filter=" + expected_filter)
        notice = "Not run (Android native tests are not available through bdev):"
        for output in (result.stderr, summary):
            self.assertIn(notice, output)
            self.assertIn("  browser/extensions/android/n_unittest.cc", output)

    def test_unverified_counts_do_not_claim_all_tests_passed(self):
        self.on_test_branch()
        self.add_tests("junit")
        result = self.summary_run(FAKE_RESULTS="none")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed; counts unavailable", result.stdout)
        self.assertNotIn("✅", result.stdout)

    def test_failed_test_counts_appear_in_the_final_summary(self):
        self.on_test_branch()
        self.add_tests("junit", "native")
        result, document = self.local(FAKE_RESULTS="fail")
        self.assertEqual(result.returncode, 5, result.stderr)
        summary = document["error"]["message"].split("Test summary:", 1)[1]
        self.assertIn("2 run, 1 passed, 1 failed, 0 skipped; phase failed", summary)
        self.assertNotIn("✅", summary)
        self.assertIn("browser/extensions/android/n_unittest.cc", summary)

    def test_a_failing_phase_does_not_stop_the_others_and_fails_the_command(self):
        self.on_test_branch()
        self.add_tests("junit", "unit")
        result, document = self.local(FAKE_MAC_EXIT="2")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "CHILD_FAILED"))
        phases = document["error"]["details"]["phases"]
        self.assertEqual([(p["suite"], p["status"]) for p in phases],
                         [("brave_junit_tests", "passed"), ("brave_unit_tests", "failed")])
        self.assertEqual(phases[1]["child_exit_code"], 2)
        self.assertIn("failed; counts unavailable", document["error"]["message"])

    def test_android_problems_stop_everything_before_any_build(self):
        self.setup_support(ref="v155")
        self.add_tests("java", "unit")
        result, document = self.local()
        self.assertEqual(document["error"]["code"], "DEPENDENCY_INCOMPATIBLE")
        self.assertEqual(self.runner_calls(), [])

        self.on_test_branch_again = subprocess.run(["git", "-C", str(self.wc()), "switch", "-q", BRANCH], check=True)
        result, document = self.local(devices=DEVICES_TWO)
        self.assertEqual(document["error"]["code"], "DEVICE_AMBIGUOUS")
        self.assertEqual(self.runner_calls(), [], "the device is chosen before the first build")

    def test_plan_lists_the_phases_without_running_or_changing_anything(self):
        self.on_test_branch()
        self.add_tests("java", "unit")
        result, document = self.local("--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["suite"] for p in document["data"]["discovery"]["phases"]],
                         ["brave_java_unit_tests", "brave_unit_tests"])
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse(self.overlay_file.exists())

    def test_nothing_modified_is_a_successful_no_op(self):
        result, document = self.local()
        self.assertEqual((result.returncode, document["data"]["phases"]), (0, []))
        self.assertEqual(self.runner_calls(), [])

    def test_a_target_limits_the_run_to_that_platforms_suites(self):
        self.on_test_branch()
        self.add_tests("junit", "unit")
        result, document = self.local("android", devices="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["suite"] for p in document["data"]["phases"]], ["brave_junit_tests"])
        result, document = self.local("mac")
        self.assertEqual([p["suite"] for p in document["data"]["phases"]], ["brave_unit_tests"])
        self.assertEqual(len(self.runner_calls()), 2)

    def test_a_target_without_matching_tests_is_a_no_op_and_an_unknown_target_is_refused(self):
        self.add_tests("unit")
        result, document = self.local("android")
        self.assertEqual((result.returncode, document["data"]["phases"]), (0, []))
        self.assertEqual(self.runner_calls(), [])
        result, document = self.local("ios")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))

    def test_a_setup_error_stops_the_remaining_phases_and_is_reported_as_itself(self):
        self.on_test_branch()
        self.add_tests("junit", "unit")
        self.overlay_file.parent.mkdir(parents=True, exist_ok=True)
        self.overlay_file.write_text("local work\n")
        result, document = self.local()
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertEqual(document["error"]["details"]["not_run"], ["brave_junit_tests", "brave_unit_tests"])
        summary = document["error"]["message"].split("Test summary:", 1)[1]
        self.assertIn("brave_junit_tests", summary)
        self.assertIn("brave_unit_tests", summary)
        self.assertEqual(summary.count("not run"), 2)
        self.assertNotIn("✅", summary)
        self.assertEqual(self.runner_calls(), [])

    def test_the_failure_message_names_each_failed_phase(self):
        self.on_test_branch()
        self.add_tests("junit", "unit")
        result, document = self.local(FAKE_MAC_EXIT="2")
        self.assertIn("1 of 2 test phase(s) failed:\n  mac brave_unit_tests:", document["error"]["message"])
