# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""`bdev test-local`: modified tests become suite phases that run through the existing test paths."""

import subprocess

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
        self.assertEqual([w["code"] for w in document["warnings"]], ["TEST_UNMAPPED"])
        self.assertIn("--filter=FooTest.*", result.stderr, "the filters run are in the log")
        self.assertIn("Test phase 2/3", result.stderr)

    def test_a_host_only_branch_needs_no_device(self):
        self.on_test_branch()
        self.add_tests("junit")
        result, document = self.local(devices="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.adb_calls(), [])

    def test_a_failing_phase_does_not_stop_the_others_and_fails_the_command(self):
        self.on_test_branch()
        self.add_tests("junit", "unit")
        result, document = self.local(FAKE_MAC_EXIT="2")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "CHILD_FAILED"))
        phases = document["error"]["details"]["phases"]
        self.assertEqual([(p["suite"], p["status"]) for p in phases],
                         [("brave_junit_tests", "passed"), ("brave_unit_tests", "failed")])
        self.assertEqual(phases[1]["child_exit_code"], 2)

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
