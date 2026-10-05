# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""`bdev test` without a suite: changed or named tests become suite phases that run through the single-suite path."""

import json
import subprocess
import shutil
import unittest
from pathlib import Path

from tests.integration.test_android_tests import BRANCH, DEVICES_TWO, ONE_DEVICE, AndroidTestsTestCase

JAVA = "package org.example.app;\n\npublic class %s {}\n"


class TestChangedTests(AndroidTestsTestCase):
    @unittest.skipUnless(shutil.which('node'), 'needs Node for the device adapter fixture')
    def test_all_devices_runs_device_suite_once_and_keeps_host_suite(self):
        self.on_test_branch()
        self.add_tests('java', 'junit')
        result, document = self.local('android', '--all-devices', devices=DEVICES_TWO, FAKE_ADAPTER_NODE=shutil.which('node'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([phase['status'] for phase in document['data']['phases']], ['passed', 'passed'])
        self.assertEqual(len(document['data']['phases'][1]['devices']), 2)

    def setUp(self):
        super().setUp()
        self.git("branch", "base-ref")

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                        "-C", str(self.core), *args], check=True, capture_output=True)

    def add_tests(self, *names):
        files = {"junit": "android/junit/src/org/example/app/BarUnitTest.java",
                 "java": "android/javatests/org/example/app/FooTest.java",
                 "unit": "browser/foo_unittest.cc", "browser": "browser/foo_browsertest.cc", "native": "browser/extensions/android/n_unittest.cc"}
        for name in names:
            path = self.core / files[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(JAVA % path.stem if name in ("junit", "java") else "TEST_F(%s, One) {}\n" % (
                "FooBrowserTest" if name == "browser" else "FooUnitTest"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "tests")

    def local(self, *args, devices=ONE_DEVICE, **env):
        return self.document("test", "--base", "base-ref", *args, env=self.env(FAKE_ADB_DEVICES=devices, **env))

    def test_runs_each_modified_suite_with_its_filter_in_order(self):
        self.on_test_branch()
        self.add_tests("java", "junit", "unit", "native")
        result, document = self.local("android", "--device", "emulator-5554", devices=DEVICES_TWO)
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
        runs = [call["argv"][call["argv"].index("test") + 1:][:2] for call in self.runner_calls()]
        self.assertEqual(runs, [["brave_junit_tests", "--filter=org.example.app.BarUnitTest.*"],
                                ["brave_java_unit_tests", "--filter=FooTest.*"]])
        device_run = self.runner_calls()[1]["argv"]
        self.assertEqual(device_run[device_run.index("--device") + 1], "emulator-5554")
        self.assertEqual([p["status"] for p in document["data"]["phases"]], ["passed"] * 2)
        self.assertEqual([item["path"] for item in document["data"]["discovery"]["unmapped"]],
                         ["browser/extensions/android/n_unittest.cc"])
        self.assertEqual(result.stderr.count("browser/extensions/android/n_unittest.cc"), 1, "listed once, not repeated")
        self.assertIn("Not run (Android native tests are not available through bdev):", result.stderr)
        self.assertIn("--filter=FooTest.*", result.stderr, "the filters run are in the log")
        self.assertIn("Phase 2/2: android brave_java_unit_tests", result.stderr)
        self.assertEqual([item["path"] for item in document["data"]["discovery"]["deselected"]], ["browser/foo_unittest.cc"])
        self.assertIn("Not run (mac tests are outside the requested android run):", result.stderr)

    def test_a_host_only_branch_needs_no_device(self):
        self.on_test_branch()
        self.add_tests("junit")
        result, document = self.local("android", devices="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.adb_calls(), [])

    def summary_run(self, **env):
        return self.sandbox.bdev("--config", self.config, "--checkout", "main", "test", "android",
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
        result, document = self.local("android", FAKE_RESULTS="fail")
        self.assertEqual(result.returncode, 5, result.stderr)
        summary = document["error"]["message"].split("Test summary:", 1)[1]
        self.assertIn("2 run, 1 passed, 1 failed, 0 skipped; phase failed", summary)
        self.assertNotIn("✅", summary)
        self.assertIn("browser/extensions/android/n_unittest.cc", summary)

    def test_a_failing_phase_does_not_stop_the_others_and_fails_the_command(self):
        self.on_test_branch()
        self.add_tests("unit", "browser")
        result, document = self.local("mac", FAKE_MAC_EXIT="2")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "CHILD_FAILED"))
        phases = document["error"]["details"]["phases"]
        self.assertEqual([(p["suite"], p["status"]) for p in phases],
                         [("brave_unit_tests", "failed"), ("brave_browser_tests", "failed")])
        self.assertEqual(phases[1]["child_exit_code"], 2)
        self.assertIn("failed; counts unavailable", document["error"]["message"])

    def test_android_problems_stop_everything_before_any_build(self):
        self.setup_support(ref="v155")
        self.add_tests("java", "unit")
        result, document = self.local("android")
        self.assertEqual(document["error"]["code"], "DEPENDENCY_INCOMPATIBLE")
        self.assertEqual(self.runner_calls(), [])

        self.on_test_branch_again = subprocess.run(["git", "-C", str(self.wc()), "switch", "-q", BRANCH], check=True)
        result, document = self.local("android", devices=DEVICES_TWO)
        self.assertEqual(document["error"]["code"], "DEVICE_AMBIGUOUS")
        self.assertEqual(self.runner_calls(), [], "the device is chosen before the first build")

    def test_plan_lists_the_phases_without_running_or_changing_anything(self):
        self.on_test_branch()
        self.add_tests("java", "junit")
        result, document = self.local("android", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["suite"] for p in document["data"]["discovery"]["phases"]],
                         ["brave_junit_tests", "brave_java_unit_tests"])
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
        self.assertIn("No tests were selected", document["data"].get("text", "") + result.stderr + result.stdout)
        self.assertEqual(self.runner_calls(), [])
        result, document = self.local("ios")
        self.assertEqual(document["error"]["code"], "UNSUPPORTED_CAPABILITY")

    def test_forwarded_platform_selects_the_same_tests_in_plan_and_execution(self):
        self.on_test_branch()
        self.add_tests("junit", "unit")
        for args in (("--target_os=android",), ("--target_os", "android")):
            with self.subTest(args=args):
                result, plan = self.local(*args, "--plan")
                self.assertEqual(result.returncode, 0, result.stderr)
                phases = plan["data"]["discovery"]["phases"]
                self.assertEqual([(p["target"], p["suite"]) for p in phases],
                                 [("android", "brave_junit_tests")])
                result, run = self.local(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(run["data"]["discovery"]["phases"], phases)

    def test_forwarded_platform_conflict_is_rejected_even_in_a_plan(self):
        self.add_tests("unit")
        result, document = self.local("mac", "--target_os=android", "--plan")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "SELECTOR_CONFLICT"))
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse(self.overlay_file.exists())

    def test_forwarded_platform_limits_explicit_file_selection(self):
        self.add_tests("unit")
        result = self.file_run("--file", "browser/foo_unittest.cc", "--target_os=android", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        discovery = json.loads(result.stdout)["data"]["discovery"]
        self.assertEqual(discovery["phases"], [])
        self.assertEqual(len(discovery["deselected"]), 1)
        self.assertEqual(self.runner_calls(), [])

    def test_a_setup_error_stops_the_remaining_phases_and_is_reported_as_itself(self):
        self.on_test_branch()
        self.add_tests("junit", "java")
        self.overlay_file.parent.mkdir(parents=True, exist_ok=True)
        self.overlay_file.write_text("local work\n")
        result, document = self.local("android")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertEqual(document["error"]["details"]["not_run"], ["brave_junit_tests", "brave_java_unit_tests"])
        summary = document["error"]["message"].split("Test summary:", 1)[1]
        self.assertIn("brave_junit_tests", summary)
        self.assertIn("brave_java_unit_tests", summary)
        self.assertEqual(summary.count("not run"), 2)
        self.assertNotIn("✅", summary)
        self.assertEqual(self.runner_calls(), [])

    def test_the_failure_message_names_each_failed_phase(self):
        self.on_test_branch()
        self.add_tests("unit", "browser")
        result, document = self.local("mac", FAKE_MAC_EXIT="2")
        self.assertIn("2 of 2 test phase(s) failed:\n  mac brave_unit_tests:", document["error"]["message"])

    def parse_error(self, *args, **kwargs):
        result, document = self.local(*args, **kwargs)
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"), result.stderr)
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse(self.overlay_file.exists())
        return document["error"]["message"]

    def test_selectors_that_conflict_are_refused_before_any_work(self):
        self.on_test_branch()
        self.add_tests("unit")
        self.assertIn("cannot be combined", self.parse_error("brave_unit_tests", "--file", "browser/foo_unittest.cc"))
        self.assertIn("cannot be combined", self.parse_error("mac", "brave_unit_tests"))
        self.assertIn("cannot be combined", self.parse_error("--file", "browser/foo_unittest.cc"))
        self.assertIn("--filter narrows a named suite", self.parse_error("mac", "--filter", "Foo.*"))

    def test_default_discovery_uses_the_host_platform_and_reports_other_platform_tests(self):
        self.on_test_branch()
        self.add_tests("junit", "unit")
        result, document = self.local("--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["suite"] for p in document["data"]["discovery"]["phases"]], ["brave_unit_tests"])
        self.assertEqual([item["path"] for item in document["data"]["discovery"]["deselected"]],
                         ["android/junit/src/org/example/app/BarUnitTest.java"])
        self.assertIn("Not run (android tests are outside the requested mac run):", result.stderr)

    def test_an_empty_selection_says_so_and_runs_nothing(self):
        self.add_tests("unit")
        result, document = self.local("android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No tests were selected.", result.stderr)
        self.assertNotIn("passed", result.stdout.lower().replace("passed=", ""))
        self.assertEqual(self.runner_calls(), [])

    def file_run(self, *args, cwd=None, **env):
        return self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "test", *args,
                                 cwd=cwd or self.core, env=self.env(FAKE_ADB_DEVICES=ONE_DEVICE, **env))

    def test_a_file_runs_its_tests_from_a_relative_path_whether_or_not_it_changed(self):
        self.add_tests("unit")
        (self.core / "browser" / "old_unittest.cc").write_text("TEST_F(OldUnitTest, One) {}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "more")
        self.git("branch", "-f", "base-ref", "HEAD")
        result = self.file_run("--file", "old_unittest.cc", cwd=self.core / "browser")
        document = json.loads(result.stdout)
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
        self.assertEqual([(p["suite"], p["filter"]) for p in document["data"]["phases"]],
                         [("brave_unit_tests", "OldUnitTest.*")])
        self.assertEqual([call["argv"][call["argv"].index("test") + 1:][:2] for call in self.runner_calls()],
                         [["brave_unit_tests", "--filter=OldUnitTest.*"]])

    def test_a_file_decides_the_platform_unless_one_is_named(self):
        self.on_test_branch()
        self.add_tests("junit")
        result = self.file_run("--plan", "--file", "android/junit/src/org/example/app/BarUnitTest.java")
        document = json.loads(result.stdout)
        self.assertEqual([p["suite"] for p in document["data"]["discovery"]["phases"]], ["brave_junit_tests"])
        result = self.file_run("mac", "--plan", "--file", "android/junit/src/org/example/app/BarUnitTest.java")
        self.assertEqual(json.loads(result.stdout)["data"]["discovery"]["phases"], [])
        self.assertIn("No tests were selected.", result.stderr)

    def test_files_outside_the_checkout_or_missing_are_refused_and_unsupported_files_are_reported(self):
        self.add_tests("unit")
        outside = self.sandbox.root / "elsewhere_unittest.cc"
        outside.write_text("TEST(A, B) {}\n")
        result = self.file_run("--file", str(outside))
        self.assertEqual((result.returncode, json.loads(result.stdout)["error"]["code"]), (2, "INVALID_INPUT"))
        self.assertIn("outside the selected checkout", json.loads(result.stdout)["error"]["message"])
        result = self.file_run("--file", "browser/missing_unittest.cc")
        self.assertIn("is not a file", json.loads(result.stdout)["error"]["message"])
        (self.core / "README.md").write_text("x\n")
        result = self.file_run("--plan", "--file", "README.md")
        document = json.loads(result.stdout)
        self.assertEqual(document["data"]["discovery"]["unmapped"],
                         [{"path": "README.md", "reason": "not a recognized test file"}])
        self.assertEqual(self.runner_calls(), [])
