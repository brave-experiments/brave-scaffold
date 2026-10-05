# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""iOS Simulator sync and readiness with fake Xcode tools and a fake package command."""

import json
import os
import unittest
from pathlib import Path

from tests.integration.test_build import SKIP, BuildTestCase
from tests.schema_validation import Validator
from tests.ios_fixtures import install_build_hook, install_fake_xcode, make_ios_project, simulator_json

IOS_HOOK = """
if "sync" in argv:
    requested = [a.split("=", 1)[1] for a in argv if a.startswith("--target_os=")]
    gclient = os.path.join(os.path.dirname(os.environ["BRAVE_CORE_DIR"]), "..", ".gclient")
    with open(gclient, "a") as stream:
        stream.write("target_os = %r\\n" % (requested[0].split(",") if requested else [],))
    if not os.environ.get("FAKE_NO_BOOTSTRAP") and "ios" in requested[0].split(","):
        core = os.environ["BRAVE_CORE_DIR"]
        link = os.path.join(os.path.dirname(core), "out", "ios_current_link")
        for name in ("BraveCore", "NalaAssets", "PartitionAllocSupport"):
            os.makedirs(os.path.join(link, name + ".xcframework"), exist_ok=True)
            open(os.path.join(link, name + ".xcframework", "Info.plist"), "w").write("<plist/>")
        open(os.path.join(link, "args.xcconfig"), "w").write("")
        configuration = os.path.join(core, "ios", "brave-ios", "App", "Configuration")
        os.makedirs(configuration, exist_ok=True)
        open(os.path.join(configuration, "LLDBInit"), "w").write("")
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class IosTestCase(BuildTestCase):
    bootstrapped = True

    def setUp(self):
        super().setUp()
        install_fake_xcode(self.sandbox)
        self.xcodebuild_hook = install_build_hook(self.sandbox)
        make_ios_project(self.core, bootstrapped=self.bootstrapped)
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['ios']\n")
        self.hook = self.sandbox.hook(IOS_HOOK)

    def env(self, **extra):
        return super().env(**{"FAKE_SIMCTL_JSON": simulator_json(), "FAKE_XCODEBUILD_HOOK": self.xcodebuild_hook,
                              **extra})

    def document(self, *args, **kwargs):
        result, document = super().document(*args, **kwargs)
        self.assertEqual(Validator().problems(document), [], "the result must match the published schema")
        return result, document

    def statuses(self, document):
        return {check["name"]: check["status"] for check in document["checks"]}

    def doctor(self, **extra):
        result, document = self.document("doctor", "ios", env=self.env(**extra))
        return result, document, self.statuses(document)

    def output_states(self):
        directory = self.sandbox.config.parent / ".bdev" / "outputs"
        return [json.loads(path.read_text()) for path in sorted(directory.glob("*/*.json"))]

    def xcode_calls(self):
        return [record for record in self.sandbox.records() if record["tool"] == "xcodebuild"]

    def simctl_calls(self):
        return [record["argv"] for record in self.sandbox.records() if record["tool"] == "xcrun"]

    def derived(self, name="ios_Debug_xcode_derived_data"):
        return (self.src / "out" / name).resolve()

    def app(self, derived=None):
        return (derived or self.derived()) / "Build" / "Products" / "Debug-iphonesimulator" / "Client.app"


class IosSyncTests(IosTestCase):
    bootstrapped = False

    def test_sync_adds_ios_to_the_existing_targets_and_checks_core_bootstrapped_the_project(self):
        (self.src.parent / ".gclient").write_text("target_os = ['android']\n")
        result, document = self.document("sync", "ios")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", "--target_os=android,ios"])

    def test_sync_fails_with_the_bootstrap_repair_when_core_leaves_the_project_unbootstrapped(self):
        result, document = self.document("sync", "ios", env=self.env(FAKE_NO_BOOTSTRAP="1"))
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertIn(["bdev", "bpm", "run", "ios_bootstrap"], [r["argv"][:4] for r in document["error"]["repairs"]])

    def test_sync_ios_refuses_options_that_defeat_the_bootstrap_hook_or_the_target_list(self):
        for option in ("--nohooks", "--target_os=ios"):
            result, document = self.document("sync", "ios", option)
            self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT", option)
        self.assertEqual(self.node_calls(), [])

    def test_sync_combines_mobile_targets_in_a_fixed_order(self):
        result, document = self.document("sync", "ios,android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", "--target_os=android,ios"])

    def test_sync_plan_names_the_arguments_and_runs_nothing(self):
        result, document = self.document("sync", "ios", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--target_os=ios", document["data"]["plan"]["argv_arguments"])
        self.assertEqual(self.node_calls(), [])


class IosDoctorTests(IosTestCase):
    def test_a_bootstrapped_checkout_with_a_compatible_simulator_is_ready(self):
        result, document, statuses = self.doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("ios-xcodebuild", "ios-simulator-sdk", "ios-gclient-target", "ios-project", "ios-bootstrap",
                     "ios-simulator"):
            self.assertEqual(statuses[name], "pass", name)

    def test_each_missing_prerequisite_is_a_blocker_with_its_own_check(self):
        for extra, name in ((dict(FAKE_NO_XCODEBUILD="1"), "ios-xcodebuild"),
                            (dict(FAKE_NO_SIMULATOR_SDK="1"), "ios-simulator-sdk"),
                            (dict(FAKE_SIMCTL_FAIL="1"), "ios-simulator"),
                            (dict(FAKE_SIMCTL_JSON=simulator_json(old_only=True)), "ios-simulator")):
            with self.subTest(name=name, extra=list(extra)):
                result, document, statuses = self.doctor(**extra)
                self.assertEqual((result.returncode, statuses[name]), (3, "blocker"))

    def test_an_unbootstrapped_checkout_gets_the_bootstrap_repair(self):
        import shutil
        shutil.rmtree(self.src / "out" / "ios_current_link")
        result, document, statuses = self.doctor()
        self.assertEqual(statuses["ios-bootstrap"], "blocker")
        check = next(c for c in document["checks"] if c["name"] == "ios-bootstrap")
        self.assertEqual(check["repairs"][0]["argv"][:4], ["bdev", "bpm", "run", "ios_bootstrap"])

    def test_a_checkout_without_an_ios_target_gets_the_sync_repair(self):
        (self.src.parent / ".gclient").write_text("target_os = []\n")
        result, document, statuses = self.doctor()
        self.assertEqual(statuses["ios-gclient-target"], "blocker")
        check = next(c for c in document["checks"] if c["name"] == "ios-gclient-target")
        self.assertEqual(check["repairs"][0]["argv"][:3], ["bdev", "sync", "ios"])


class IosBuildTests(IosTestCase):
    def test_build_runs_xcodebuild_for_the_debug_scheme_and_verifies_the_app(self):
        result, document = self.document("build", "ios")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
        (call,) = self.xcode_calls()
        project = (self.core / "ios" / "brave-ios" / "App" / "Client.xcodeproj").resolve()
        self.assertEqual(call["argv"], [
            "-project", str(project), "-scheme", "Debug", "-configuration", "Debug", "-sdk", "iphonesimulator",
            "-destination", "platform=iOS Simulator,id=BBBB-PHONE", "-derivedDataPath", str(self.derived()),
            "build"])
        self.assertEqual(os.path.realpath(call["cwd"]), str(self.core.resolve()))
        self.assertEqual([a["path"] for a in document["artifacts"]], [str(self.app())])
        self.assertEqual(document["data"]["build"]["effective"]["gn_output"],
                         str((self.src / "out" / "ios_Debug_arm64_simulator").resolve()))
        self.assertEqual(self.simctl_calls(), [], "build never touches a simulator")
        self.assertEqual(self.node_calls(), [], "iOS does not go through Core's package build command")

    def test_the_build_is_recorded_for_both_outputs(self):
        self.document("build", "ios")
        states = self.output_states()
        self.assertEqual(sorted(os.path.basename(s["output_dir"]) for s in states),
                         ["ios_Debug_arm64_simulator", "ios_Debug_xcode_derived_data"])
        for state in states:
            self.assertEqual(state["success"]["artifact"]["target"], "ios")
            self.assertFalse(state["needs_revalidation"])

    def test_unknown_arguments_go_to_xcodebuild_unchanged_before_the_action(self):
        result, document = self.document("build", "ios", "-jobs", "2", "CODE_SIGNING_ALLOWED=NO")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.xcode_calls()[0]["argv"][-5:],
                         ["-derivedDataPath", str(self.derived()), "-jobs", "2", "CODE_SIGNING_ALLOWED=NO", "build"][-5:])

    def test_a_forwarded_derived_data_path_replaces_the_default_and_selects_the_output(self):
        other = self.sandbox.root / "custom-derived"
        result, document = self.document("build", "ios", "-derivedDataPath", str(other))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.xcode_calls()[0]["argv"]
        self.assertEqual(argv.count("-derivedDataPath"), 1)
        self.assertEqual([a["path"] for a in document["artifacts"]], [str(self.app(other.resolve()))])
        self.assertFalse(self.derived().exists())

    def test_options_that_change_what_is_built_fail_before_anything_runs(self):
        cases = ((("-sdk", "iphoneos"), "SELECTOR_CONFLICT"), (("-scheme", "Release"), "SELECTOR_CONFLICT"),
                 (("-configuration", "Release"), "SELECTOR_CONFLICT"), (("--configuration", "release"), "UNSUPPORTED_CAPABILITY"),
                 (("--offline",), "INVALID_INPUT"), (("--force-gn",), "INVALID_INPUT"),
                 (("--device", "iphoneos"), "INVALID_INPUT"),
                 (("--device", "iPhone 16", "-destination", "x"), "SELECTOR_CONFLICT"),
                 (("-derivedDataPath",), "INVALID_INPUT"))
        for arguments, code in cases:
            with self.subTest(arguments=arguments):
                result, document = self.document("build", "ios", *arguments)
                self.assertEqual(document["error"]["code"], code)
                self.assertEqual(result.returncode, 2)
        self.assertEqual(self.xcode_calls(), [])
        self.assertEqual(self.output_states(), [])

    def test_device_chooses_the_simulator_by_name_or_udid(self):
        for requested in ("iPhone 16 Pro", "CCCC-PHONE"):
            self.sandbox.record.unlink(missing_ok=True)
            result, document = self.document("build", "ios", "--device", requested)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("platform=iOS Simulator,id=CCCC-PHONE", self.xcode_calls()[0]["argv"])

    def test_an_unavailable_simulator_fails_before_the_build(self):
        result, document = self.document("build", "ios", "--device", "iPhone 8")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEVICE_UNAVAILABLE"))
        self.assertEqual(self.xcode_calls(), [])

    def test_a_failed_build_reports_the_child_status_and_marks_both_outputs_for_revalidation(self):
        self.assertEqual(self.document("build", "ios")[0].returncode, 0)
        result, document = self.document("build", "ios", env=self.env(FAKE_XCODEBUILD_EXIT="65"))
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "CHILD_FAILED"))
        states = self.output_states()
        self.assertEqual(len(states), 2)
        for state in states:
            self.assertTrue(state["needs_revalidation"])
            self.assertIsNotNone(state["success"], "the earlier success stays as history")
        result, document = self.document("run", "ios")
        self.assertEqual(result.returncode, 0, "an older app may still run after inspection")

    def test_an_app_that_core_did_not_produce_is_not_accepted(self):
        for extra, code in ((dict(FAKE_NO_APP="1"), "ARTIFACT_MISSING"),
                            (dict(FAKE_IOS_PLATFORM="iPhoneOS"), "ARTIFACT_MISMATCH"),
                            (dict(FAKE_IOS_BUNDLE_ID="org.example.other"), "ARTIFACT_MISMATCH"),
                            (dict(FAKE_GN_NAME="ios_Debug_elsewhere"), "ARTIFACT_MISMATCH")):
            with self.subTest(extra=extra):
                result, document = self.document("build", "ios", env=self.env(**extra))
                self.assertEqual((result.returncode, document["error"]["code"]), (5, code))

    def test_modes_that_build_nothing_leave_the_artifact_unresolved(self):
        self.assertEqual(self.document("build", "ios")[0].returncode, 0)
        before = self.output_states()
        result, document = self.document("build", "ios", "-showBuildSettings")
        self.assertEqual((result.returncode, document["warnings"][0]["code"]), (0, "ARTIFACT_UNRESOLVED"))
        self.assertEqual(document["artifacts"], [])
        self.assertEqual([s["needs_revalidation"] for s in self.output_states()], [False, False])
        self.assertEqual([s["success"] for s in self.output_states()], [s["success"] for s in before])
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("build-run", "ios", "-showBuildSettings")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_UNRESOLVED"))
        self.assertEqual(self.simctl_calls(), [], "an older app is not a substitute for an unresolved output")

    def test_plan_shows_the_steps_and_runs_nothing(self):
        result, document = self.document("build-run", "ios", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [step["name"] for step in document["data"]["plan"]["steps"]]
        for name in ("select-simulator", "xcodebuild", "verify-output", "boot-simulator", "install-app", "launch-app"):
            self.assertIn(name, names)
        self.assertEqual((self.xcode_calls(), self.simctl_calls(), self.output_states()), ([], [], []))

    def test_sync_build_syncs_first_and_then_builds(self):
        (self.src.parent / ".gclient").write_text("target_os = []\n")
        result, document = self.document("sync-build", "ios")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", "--target_os=ios"])
        self.assertEqual(len(self.xcode_calls()), 1)

    def test_tests_are_not_available_for_ios(self):
        result, document = self.document("test", "ios", "brave_unit_tests")
        self.assertEqual(document["error"]["code"], "UNSUPPORTED_CAPABILITY")

    def test_a_checkout_that_is_not_ready_builds_nothing(self):
        import shutil
        shutil.rmtree(self.src / "out" / "ios_current_link")
        result, document = self.document("build", "ios")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
        self.assertEqual(self.xcode_calls(), [])

    def test_clean_removes_the_simulator_outputs_the_build_made(self):
        self.document("build", "ios")
        result, document = self.document("clean", "ios", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(e["name"] for e in document["data"]["entries"]),
                         ["ios_Debug_arm64_simulator", "ios_Debug_xcode_derived_data"])
        self.assertFalse(self.derived().exists())


class IosRunTests(IosTestCase):
    def built(self):
        self.assertEqual(self.document("build", "ios")[0].returncode, 0)
        self.sandbox.record.unlink(missing_ok=True)

    def test_run_boots_installs_and_launches_on_the_chosen_simulator(self):
        self.built()
        result, document = self.document("run", "ios")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.simctl_calls(), [
            ["simctl", "boot", "BBBB-PHONE"], ["simctl", "bootstatus", "BBBB-PHONE", "-b"],
            ["simctl", "install", "BBBB-PHONE", str(self.app())],
            ["simctl", "launch", "--terminate-running-process", "BBBB-PHONE", "com.brave.ios.browser.dev"]])
        self.assertEqual(document["data"]["run"]["launched_pid"], 4321)
        self.assertEqual(self.xcode_calls(), [], "run never builds")

    def test_run_needs_an_existing_build(self):
        result, document = self.document("run", "ios")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_MISSING"))
        self.assertEqual(self.simctl_calls(), [])

    def test_build_run_uses_the_simulator_it_built_for(self):
        result, document = self.document("build-run", "ios", "--device", "iPhone 16 Pro")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("platform=iOS Simulator,id=CCCC-PHONE", self.xcode_calls()[0]["argv"])
        self.assertTrue(all(call[2 if call[1] != "launch" else 3] == "CCCC-PHONE" for call in self.simctl_calls()
                            if call[1] in ("boot", "bootstatus", "install", "launch")))
        self.assertEqual([call[1] for call in self.simctl_calls()], ["boot", "bootstatus", "install", "launch"])

    def test_build_run_does_not_launch_when_the_build_fails(self):
        result, document = self.document("build-run", "ios", env=self.env(FAKE_XCODEBUILD_EXIT="65"))
        self.assertEqual(document["error"]["code"], "CHILD_FAILED")
        self.assertEqual(self.simctl_calls(), [])

    def test_each_simctl_failure_is_a_launch_failure_that_names_its_phase(self):
        self.built()
        for name, phase in (("BOOT", "boot-simulator"), ("INSTALL", "install-app"), ("LAUNCH", "launch-app")):
            with self.subTest(phase=phase):
                result, document = self.document("run", "ios", env=self.env(**{"FAKE_SIMCTL_FAIL_" + name: "1"}))
                self.assertEqual((result.returncode, document["error"]["code"]), (5, "LAUNCH_FAILED"))
                self.assertEqual(document["error"]["details"]["phase"], phase)

    def test_an_explicit_artifact_and_physical_device_requests(self):
        self.built()
        result, document = self.document("run", "ios", "--artifact", str(self.app()))
        self.assertEqual(result.returncode, 0, result.stderr)
        result, document = self.document("run", "ios", "--device", "physical iPhone")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
        result, document = self.document("run", "ios", "--artifact", str(self.src / "out" / "nothing.app"))
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISSING")

    def test_run_plan_names_the_simulator_and_changes_nothing(self):
        self.built()
        result, document = self.document("run", "ios", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BBBB-PHONE", result.stdout)
        self.assertEqual(self.simctl_calls(), [])


if __name__ == "__main__":
    unittest.main()
