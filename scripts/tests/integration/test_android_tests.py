# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android tests: suite routing, filter and device forwarding, the support-branch requirement, and outcomes."""

import json
import subprocess
import unittest
from pathlib import Path

from tests.android_fixtures import GIT, OVERLAY_FILES
from tests.integration.test_android import ANDROID_HOOK, DEVICES_TWO, AndroidTestCase
from tests.integration.test_build import SKIP

BRANCH = "android-testing-prototype"
ONE_DEVICE = "emulator-5554,device"

TEST_HOOK = """
core = os.environ["BRAVE_CORE_DIR"]
src = os.path.dirname(core)
build_dir = argv[argv.index("-C") + 1]
out = os.path.join(src, "out", build_dir)
os.makedirs(os.path.join(out, "bin"), exist_ok=True)
suite = argv[argv.index("test") + 1]
if not os.path.exists(os.path.join(core, "build", "commands", "lib", "androidTestMacHost.ts")):
    print("the test overlay is not applied", file=sys.stderr)
    raise SystemExit(9)
if not os.environ.get("FAKE_NO_RUNNER"):
    with open(os.path.join(out, "bin", "run_" + suite), "w") as stream:
        stream.write("#!/bin/sh\\n")
mode = os.environ.get("FAKE_RESULTS", "pass")
targets = [a.split("=", 1)[1] for a in argv if a.startswith("--json-results-file=")]
tests = {"pass": {"a.B#one": [{"status": "SUCCESS"}], "a.B#two": [{"status": "SUCCESS"}, {"status": "SUCCESS"}],
                  "a.B#three": [{"status": "SKIP"}]},
         "fail": {"a.B#one": [{"status": "SUCCESS"}], "a.B#two": [{"status": "FAILURE"}]},
         "empty": {}}
if targets and mode in tests:
    with open(targets[0], "w") as stream:
        json.dump({"per_iteration_data": [tests[mode]]}, stream)
elif targets and mode == "garbage":
    open(targets[0], "w").write("not json")
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
"""
TEST_HOOK = "if 'test' in argv:\n" + "".join("    " + line + "\n" for line in TEST_HOOK.strip().splitlines()) + "\n"


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class AndroidTestsTestCase(AndroidTestCase):
    support_overlay = True

    def setUp(self):
        super().setUp()
        self.hook = self.sandbox.hook("import json\n" + TEST_HOOK + ANDROID_HOOK)
        self.overlay_file = self.src / "brave" / "build" / "commands" / "lib" / "androidTestMacHost.ts"

    def last_argv(self):
        return self.runner_calls()[-1]["argv"][1:]

    def runner_calls(self):
        return [r for r in self.node_calls() if "test" in r["argv"]]

    def on_test_branch(self):
        self.assertEqual(self.setup_support(ref=BRANCH).returncode, 0)
        return self.wc()

    def run_tests(self, *args, devices=ONE_DEVICE, **env):
        return self.document("test", "android", *args, env=self.env(FAKE_ADB_DEVICES=devices, **env))

    def results_path(self):
        return self.src / "out" / "android_tests_Debug_arm64" / "scaffold_test_results.json"


class HostSuiteTests(AndroidTestsTestCase):
    def test_junit_runs_on_the_mac_with_the_filter_and_without_a_device(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_junit_tests", "--filter=*BraveCommandLineInitUtilTest*", devices="")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
        argv = self.last_argv()
        self.assertEqual(argv[:4], ["run", "test", "brave_junit_tests", "--filter=*BraveCommandLineInitUtilTest*"])
        for expected in ("--target_os=android", "--target_arch=arm64", "--gn=is_component_build:false",
                         "--use_remoteexec=true", "Debug"):
            self.assertIn(expected, argv)
        self.assertEqual(argv[argv.index("-C") + 1], "android_tests_Debug_arm64")
        self.assertEqual(argv[-1], "--json-results-file=%s" % self.results_path())
        for absent in ("--device", "--adb-path", "--manual_android_test_device"):
            self.assertNotIn(absent, argv)
        self.assertEqual(self.adb_calls(), [], "a host-side suite never touches adb")
        self.assertTrue(self.overlay_file.exists(), "the support overlay was applied to Core")
        self.assertEqual(document["data"]["runs_on"], "host")
        self.assertIsNone(document["data"]["device"])
        self.assertEqual({key: document["data"]["results"][key] for key in ("passed", "failed", "skipped", "ran")},
                         {"passed": 2, "failed": 0, "skipped": 1, "ran": 2})
        self.assertEqual(self.runner_calls()[-1]["cwd"], str(self.core))

    def test_junit_rejects_a_device_before_anything_runs(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_junit_tests", "--device", "emulator-5554")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse(self.overlay_file.exists())

    def test_a_configured_default_device_applies_to_device_suites_only(self):
        self.on_test_branch()
        with open(self.config, "a") as stream:
            stream.write('\n[defaults]\nandroid_device = "emulator-5554"\n')
        result, document = self.run_tests("brave_junit_tests", devices="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.adb_calls(), [])
        result, document = self.run_tests("brave_java_unit_tests", devices=DEVICES_TWO)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.last_argv()
        self.assertEqual(argv[argv.index("--device") + 1], "emulator-5554", "a device suite keeps the configured default")

    def test_the_overlay_is_applied_once_and_left_in_place(self):
        self.on_test_branch()
        self.run_tests("brave_junit_tests")
        before = self.overlay_file.read_text()
        result, document = self.run_tests("brave_junit_tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.overlay_file.read_text(), before)
        self.assertEqual(len(self.runner_calls()), 2)
        self.assertNotIn("--force_gn_gen", self.last_argv(), "current support and overlay do not regenerate GN")

    def test_offline_and_custom_output_reach_the_runner(self):
        self.on_test_branch()
        result, _ = self.run_tests("brave_junit_tests", "--offline", "-C", "Custom")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.last_argv()
        self.assertIn("--offline", argv)
        self.assertNotIn("--use_remoteexec=true", argv)
        self.assertEqual(argv.count("-C"), 1)
        self.assertEqual(argv[-1], "--json-results-file=%s" % (self.src / "out" / "Custom" / "scaffold_test_results.json"))

    def test_a_forwarded_results_file_is_kept_and_its_counts_are_not_claimed(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_junit_tests", "--json-results-file=/tmp/mine.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.last_argv()
        self.assertEqual([a for a in argv if a.startswith("--json-results-file")], ["--json-results-file=/tmp/mine.json"])
        self.assertIsNone(document["data"]["results"])


class DeviceSuiteTests(AndroidTestsTestCase):
    def test_instrumented_tests_run_on_the_selected_device_with_translated_options(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_java_unit_tests", "--filter=BraveAppearancePreferencesTest.*",
                                          "--device=emulator-5554", devices=DEVICES_TWO)
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
        argv = self.last_argv()
        self.assertEqual(argv[:5], ["run", "test", "brave_java_unit_tests", "--filter=BraveAppearancePreferencesTest.*",
                                    "--manual_android_test_device"])
        self.assertLess(argv.index("--manual_android_test_device"), argv.index("--target_os=android"))
        tail = argv.index("--device")
        self.assertGreater(tail, argv.index("Debug"), "unknown options follow every option the runner parses")
        self.assertEqual(argv[tail:tail + 3], ["--device", "emulator-5554", "--adb-path"])
        self.assertTrue(argv[tail + 3].endswith("/adb"), argv[tail + 3])
        self.assertEqual(argv[-1], "--json-results-file=%s" % self.results_path())
        self.assertEqual(sum(1 for a in argv if a.startswith("--device")), 1, "the scaffold option is not also forwarded")
        self.assertEqual(document["data"]["device"], "emulator-5554")
        self.assertEqual(document["data"]["runs_on"], "device")
        self.assertEqual(self.adb_calls(), [["devices"]], "selection only lists devices; nothing is installed")

    def test_the_only_ready_device_is_selected_like_other_device_commands(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_java_unit_tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.last_argv()
        self.assertEqual(argv[argv.index("--device") + 1], "emulator-5554")

    def test_an_ambiguous_or_missing_device_stops_before_preparing_anything(self):
        self.on_test_branch()
        for devices, code, exit_code in ((DEVICES_TWO, "DEVICE_AMBIGUOUS", 2), ("", "DEVICE_UNAVAILABLE", 3)):
            with self.subTest(code=code):
                result, document = self.run_tests("brave_java_unit_tests", devices=devices)
                self.assertEqual((result.returncode, document["error"]["code"]), (exit_code, code))
        result, document = self.run_tests("brave_java_unit_tests", "--device", "R99", devices=DEVICES_TWO)
        self.assertEqual(document["error"]["code"], "DEVICE_UNAVAILABLE")
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse(self.overlay_file.exists(), "the overlay is not applied before a device is chosen")

    def test_device_options_cannot_be_forwarded_around_the_scaffold(self):
        self.on_test_branch()
        for token in (["--device=emulator-5554"], ["--adb-path", "/x/adb"], ["--manual_android_test_device"]):
            with self.subTest(token=token):
                result, document = self.run_tests("brave_java_unit_tests", "--", *token)
                self.assertEqual((result.returncode, document["error"]["code"]), (2, "SELECTOR_CONFLICT"))
        self.assertEqual(self.runner_calls(), [])


class SupportBranchTests(AndroidTestsTestCase):
    def head_state(self, wc):
        return subprocess.run(["git", "-C", str(wc), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip(), self.head(wc)

    def assert_blocked_before_any_work(self, args, message):
        wc = self.wc()
        before = self.head_state(wc)
        for command in ((*args,), (*args, "--plan")):
            with self.subTest(command=command):
                result, document = self.run_tests(*command)
                self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEPENDENCY_INCOMPATIBLE"))
                self.assertIn(message, document["error"]["message"])
                self.assertIn(BRANCH, document["error"]["message"])
                repair = document["error"]["repairs"][0]
                self.assertEqual(repair["argv"], ["git", "-C", str(wc), "switch", BRANCH])
                self.assertTrue(repair["requires_user_action"])
        self.assertEqual(self.head_state(wc), before, "the support repository is never switched")
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse(self.overlay_file.exists())
        self.assertFalse((self.src / "SUPPORT_PATCHED").exists(), "support preparation did not run")
        self.assertFalse((self.src / "out" / "android_tests_Debug_arm64").exists())
        self.assertEqual(self.adb_calls(), [])

    def test_a_detached_head_is_refused(self):
        self.assertEqual(self.setup_support(ref="v155").returncode, 0)
        self.assertEqual(self.head_state(self.wc())[0], "HEAD")
        self.assert_blocked_before_any_work(("brave_junit_tests",), "detached HEAD")
        self.assert_blocked_before_any_work(("brave_java_unit_tests",), "detached HEAD")

    def test_another_branch_is_refused(self):
        self.assertEqual(self.setup_support(ref=BRANCH).returncode, 0)
        subprocess.run([*GIT, "-C", str(self.wc()), "switch", "-q", "-c", "other-work"], check=True)
        self.assert_blocked_before_any_work(("brave_junit_tests",), "branch other-work")

    def test_the_branch_is_checked_before_the_device_and_the_suite_options_matter(self):
        self.assertEqual(self.setup_support(ref="v155").returncode, 0)
        result, document = self.run_tests("brave_java_unit_tests", devices=DEVICES_TWO)
        self.assertEqual(document["error"]["code"], "DEPENDENCY_INCOMPATIBLE")
        self.assertEqual(self.adb_calls(), [])

    def test_a_missing_support_working_copy_points_to_setup(self):
        result, document = self.run_tests("brave_junit_tests")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEPENDENCY_INCOMPATIBLE"))
        self.assertEqual(document["error"]["repairs"][0]["argv"][:3], ["bdev", "android", "setup"])

    def test_the_default_setup_lands_on_the_required_branch(self):
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "android", "setup",
                                   "--source", str(self.support), env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head_state(self.wc())[0], BRANCH)
        result, document = self.run_tests("brave_junit_tests")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_the_selected_checkouts_working_copy_counts(self):
        self.on_test_branch()
        other = self.sandbox.make_checkout("second", git=True)
        self.assertNotEqual(other.parent.parent, self.src.parent)
        self.sandbox.register("second")
        result, document = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "second", "test",
                                             "android", "brave_junit_tests", env=self.env()), None
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "DEPENDENCY_INCOMPATIBLE")


class OutputTests(AndroidTestsTestCase):
    def test_bytecode_rewrite_details_are_shown_only_in_verbose_output_for_tests_and_apps(self):
        detail = "redirecting constructor from upstream/Class to brave/Class"
        code = 'if "test" in argv or "build" in argv:\n    print("%s")\n    print("[12/20] progress")\n' % detail
        self.hook = self.sandbox.hook("import json\n" + code + TEST_HOOK + ANDROID_HOOK)
        self.on_test_branch()
        for command in (("test", "android", "brave_junit_tests"), ("build", "android")):
            for verbosity in ("normal", "verbose"):
                with self.subTest(command=command, verbosity=verbosity):
                    result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", *command,
                                               "--verbosity", verbosity, env=self.env(FAKE_ADB_DEVICES=ONE_DEVICE))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(detail in result.stderr, verbosity == "verbose")
                    self.assertIn("[12/20] progress", result.stderr)


class OutcomeTests(AndroidTestsTestCase):
    def test_an_unsupported_suite_is_refused_first(self):
        result, document = self.run_tests("brave_browser_tests")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "UNSUPPORTED_CAPABILITY"))
        self.assertIn("brave_junit_tests", document["error"]["details"]["suites"])
        self.assertEqual(self.runner_calls(), [])

    def test_a_runner_failure_is_reported_with_its_exit_code_and_counts(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_junit_tests", FAKE_EXIT="3", FAKE_RESULTS="fail")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "CHILD_FAILED"))
        self.assertEqual(document["child_exit_code"], 3)
        self.assertEqual(document["error"]["details"]["results"]["failed"], 1)
        self.assertNotIn("passed.", result.stdout)

    def test_failures_recorded_by_a_runner_that_exits_zero_still_fail(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_junit_tests", FAKE_RESULTS="fail")
        self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]), (5, "TEST_FAILED", 0))

    def test_a_run_of_zero_tests_fails_with_filter_advice(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_junit_tests", "--filter=NoSuchTest.*", FAKE_RESULTS="empty")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "NO_TESTS_RAN"))
        self.assertIn("wildcard", document["error"]["message"])

    def test_a_missing_runner_fails_even_when_the_command_exits_zero(self):
        self.on_test_branch()
        result, document = self.run_tests("brave_junit_tests", FAKE_NO_RUNNER="1")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_MISSING"))

    def test_missing_results_are_a_warning_not_a_claim_of_success_counts(self):
        self.on_test_branch()
        for mode in ("none", "garbage"):
            with self.subTest(mode=mode):
                result, document = self.run_tests("brave_junit_tests", FAKE_RESULTS=mode)
                self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
                self.assertIn("TEST_RESULTS_UNVERIFIED", [w["code"] for w in document["warnings"]])
                self.assertIsNone(document["data"]["results"])

    def test_a_stale_results_file_is_not_mistaken_for_this_run(self):
        self.on_test_branch()
        self.results_path().parent.mkdir(parents=True, exist_ok=True)
        self.results_path().write_text(json.dumps({"per_iteration_data": [{"old": [{"status": "SUCCESS"}]}]}))
        result, document = self.run_tests("brave_junit_tests", FAKE_RESULTS="none")
        self.assertIn("TEST_RESULTS_UNVERIFIED", [w["code"] for w in document["warnings"]])

    def test_a_failed_build_marks_the_test_output_for_revalidation_and_keeps_the_app_output(self):
        self.on_test_branch()
        self.document("build", "android")
        app_output = self.src / "out" / "android_Debug_arm64"
        before = (app_output / "args.gn").read_text()
        result, document = self.run_tests("brave_junit_tests", FAKE_EXIT="1", FAKE_RESULTS="none")
        self.assertEqual(document["error"]["code"], "CHILD_FAILED")
        self.assertEqual((app_output / "args.gn").read_text(), before, "the app build output is untouched")
        self.assertTrue((app_output / "apks" / "BraveMonoarm64.apk").exists())

    def test_a_conflicting_overlay_is_not_forced(self):
        self.on_test_branch()
        self.overlay_file.parent.mkdir(parents=True, exist_ok=True)
        self.overlay_file.write_text("local work\n")
        result, document = self.run_tests("brave_junit_tests")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertEqual(self.overlay_file.read_text(), "local work\n")
        self.assertEqual(self.runner_calls(), [])

    def test_an_overlay_patch_outside_its_reviewed_list_is_not_applied(self):
        self.on_test_branch()
        wc = self.wc()
        patch = wc / "patches" / "brave-core-android-tests-on-mac.patch"
        patch.write_text(patch.read_text() + "diff --git a/package.json b/package.json\nnew file mode 100644\n"
                         "--- /dev/null\n+++ b/package.json\n@@ -0,0 +1 @@\n+{}\n")
        result, document = self.run_tests("brave_junit_tests")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertFalse(self.overlay_file.exists())
        self.assertFalse((self.core / "package.json.new").exists())
        self.assertEqual(self.runner_calls(), [])


class PlanTests(AndroidTestsTestCase):
    def plan_steps(self, *args, **kwargs):
        result, document = self.run_tests(*args, "--plan", **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return document["data"]["plan"]["steps"]

    def test_a_plan_names_the_overlay_and_device_without_changing_anything(self):
        self.on_test_branch()
        steps = self.plan_steps("brave_java_unit_tests", "--filter=X.*", devices=DEVICES_TWO)
        order = [step["name"] for step in steps]
        self.assertLess(order.index("android-support"), order.index("android-test-overlay"))
        self.assertLess(order.index("android-test-overlay"), order.index("gn-overrides"))
        self.assertLess(order.index("gn-overrides"), order.index("test"))
        self.assertIn("select-device", order)
        overlay = steps[order.index("android-test-overlay")]
        self.assertEqual((overlay["status"], overlay["writes"]),
                         ("planned", [str(self.src / name) for name in OVERLAY_FILES]))
        self.assertFalse(self.overlay_file.exists())
        self.assertEqual(self.runner_calls(), [])
        self.assertFalse((self.src / "out" / "android_tests_Debug_arm64").exists())

    def test_a_host_plan_has_no_device_step(self):
        self.on_test_branch()
        order = [step["name"] for step in self.plan_steps("brave_junit_tests", devices="")]
        self.assertNotIn("select-device", order)
        self.assertEqual(self.adb_calls(), [])

    def test_the_planned_command_is_the_dispatched_command(self):
        self.on_test_branch()
        steps = self.plan_steps("brave_java_unit_tests", "--filter=X.*")
        step = next(step for step in steps if step["name"] == "test")
        planned = step["argv"]
        self.assertEqual(step["conditional_arguments"], ["--force_gn_gen"], "the first run refreshes support")
        self.assertEqual(self.run_tests("brave_java_unit_tests", "--filter=X.*")[0].returncode, 0)
        dispatched = self.runner_calls()[-1]["argv"]
        dispatched = [a for a in dispatched if a != "--force_gn_gen"]
        self.assertEqual(planned[planned.index("run"):], dispatched[dispatched.index("run"):])
