# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Plans describe every phase of an operation and agree with what execution dispatches."""

import json
import unittest

from tests.integration.test_android import AndroidTestCase
from tests.integration.test_build import BUILD_HOOK, SKIP, BuildTestCase
from tests.support import tree_snapshot

BUILD_HOOK_AFTER_APPLY = '''
if "apply_patches" in argv:
    raise SystemExit(0)
''' + BUILD_HOOK

STEP_FIELDS = {"name", "summary", "status", "reads", "writes", "argv", "cwd", "needs", "on_failure", "cleanup", "detail",
               "conditional_arguments"}


def by_name(document):
    return {step["name"]: step for step in document["data"]["plan"]["steps"]}


def names(document):
    return [step["name"] for step in document["data"]["plan"]["steps"]]


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class MacPlanTests(BuildTestCase):
    def plan(self, *args, env=None):
        before = tree_snapshot(self.sandbox.root / "config"), tree_snapshot(self.core.parents[3])
        calls = len(self.node_calls())
        result, document = self.document(*args, "--plan", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((tree_snapshot(self.sandbox.root / "config"), tree_snapshot(self.core.parents[3])), before,
                         "planning writes nothing")
        self.assertEqual(len(self.node_calls()), calls, "planning starts no package command")
        return document

    def test_build_run_plan_covers_every_phase_with_the_same_fields(self):
        document = self.plan("build-run")
        self.assertEqual(names(document), ["environment", "tools", "readiness", "patch-preparation", "build",
                                           "verify-output", "stop-running-instances", "launch"])
        for step in document["data"]["plan"]["steps"]:
            self.assertEqual(set(step), STEP_FIELDS, step["name"])
        steps = by_name(document)
        self.assertEqual(steps["build"]["writes"], [str(self.src / "out" / "Debug_arm64")])
        self.assertEqual(steps["build"]["cwd"], str(self.core))
        self.assertIn("patch-preparation", steps["build"]["needs"])
        self.assertIn("cannot be restored", steps["build"]["on_failure"] + steps["build"]["cleanup"])
        self.assertEqual(steps["stop-running-instances"]["writes"], [])
        self.assertIn("exactly the artifact", steps["launch"]["summary"])
        self.assertIn("nothing is stopped", steps["stop-running-instances"]["on_failure"])
        self.assertIn(str(self.src / "out" / "Debug_arm64"), steps["launch"]["argv"][-1])

    def test_the_planned_build_command_is_the_one_execution_dispatches(self):
        planned = by_name(self.plan("build"))["build"]["argv"]
        result, document = self.document("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = next(item for item in self.records() if item["operation_id"] == document["operation_id"])
        dispatched = [c["argv"] for c in record["commands"] if "build" in c["argv"]]
        self.assertIn(planned, dispatched)
        step = next(item for item in record["steps"] if item["name"] == "build")
        self.assertEqual(step["writes"], [str(self.src / "out" / "Debug_arm64")])
        self.assertEqual(step["argv"], planned)

    def add_unrecorded_patches(self, count):
        """Patches without metadata that target clean tracked files."""
        targets = ["planned/file_%03d.cc" % index for index in range(count)]
        (self.src / "planned").mkdir(exist_ok=True)
        for target in targets:
            (self.src / target).write_text("upstream\n")
        self.sandbox.commit_all("main")
        for target in targets:
            (self.core / "patches" / target.replace("/", "-")).with_suffix(".patch").write_text(
                "diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-upstream\n+patched\n" % ((target,) * 4))
        return [str(self.src / target) for target in targets]

    def test_a_new_patch_lists_the_files_it_will_write(self):
        (target,) = self.add_unrecorded_patches(1)
        step = by_name(self.plan("build"))["patch-preparation"]
        self.assertEqual(step["status"], "planned")
        self.assertIn(target, step["writes"])
        self.assertIn(str(self.src / "chrome" / "VERSION"), step["writes"], "Core's patch step also updates the version")

    def test_more_than_fifty_affected_paths_are_all_listed_and_the_text_summarises(self):
        targets = self.add_unrecorded_patches(60)
        document = self.plan("build")
        step = by_name(document)["patch-preparation"]
        self.assertTrue(set(targets) <= set(step["writes"]))
        result = self.sandbox.bdev("--config", self.config, "--checkout", "main", "build", "--plan", env=self.env())
        self.assertIn("writes 122 file", result.stdout)  # 60 targets, 60 metadata files, two version files
        self.assertLess(result.stdout.count("planned/file_"), 60)

    def test_an_explicit_force_gn_is_in_the_planned_command_and_matches_dispatch(self):
        planned = by_name(self.plan("build", "--force-gn"))["build"]
        self.assertIn("--force_gn_gen", planned["argv"])
        self.assertEqual(planned["conditional_arguments"], [])
        result, document = self.document("build", "--force-gn")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.node_calls()[-1]["argv"][1:], planned["argv"][2:])

    def test_regeneration_that_preparation_may_cause_is_conditional_and_then_dispatched(self):
        plain = by_name(self.plan("build"))["build"]
        self.assertNotIn("--force_gn_gen", plain["argv"])
        self.assertEqual(plain["conditional_arguments"], [])
        self.add_unrecorded_patches(1)
        self.hook = self.sandbox.hook(BUILD_HOOK_AFTER_APPLY)
        planned = by_name(self.plan("build"))["build"]
        self.assertEqual(planned["conditional_arguments"], ["--force_gn_gen"])
        self.assertNotIn("--force_gn_gen", planned["argv"])
        result, document = self.document("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.node_calls()[-1]["argv"][1:], planned["argv"][2:] + ["--force_gn_gen"])

    def test_decisions_that_depend_on_a_sync_are_unresolved_until_it_ran(self):
        self.add_unrecorded_patches(1)
        steps = by_name(self.plan("sync-build"))
        self.assertEqual(steps["patch-preparation"]["status"], "unresolved")
        self.assertIn("after the sync", steps["patch-preparation"]["detail"])
        self.assertEqual(steps["build"]["conditional_arguments"], ["--force_gn_gen"])

    def test_the_sync_plan_lists_the_repositories_a_sync_can_reset(self):
        self.sandbox.add_dependency("main")
        step = by_name(self.plan("sync"))["sync"]
        self.assertIn(str(self.src / "v8"), step["writes"])
        self.assertIn(str(self.src), step["writes"])

    def records(self):
        directory = self.sandbox.config.parent / ".bdev" / "operations"
        return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]

    def test_unresolved_prerequisites_are_reported_not_raised(self):
        self.sandbox.configure_rbe("main", siso_cache_dir=str(self.sandbox.root / "missing-cache"))
        (self.core / "third_party" / "node" / "node-mac-arm64" / "bin" / "node").unlink()
        document = self.plan("build")
        steps = by_name(document)
        self.assertEqual(steps["readiness"]["status"], "blocked")
        self.assertIn("rbe-siso-cache", steps["readiness"]["detail"])
        self.assertEqual(steps["tools"]["status"], "blocked")
        self.assertEqual(steps["build"]["status"], "planned", "the command is still shown")
        self.assertIsNone(steps["build"]["argv"], "the final command cannot be resolved without local tools")
        self.assertTrue(steps["build"]["detail"])

    def test_local_compilation_does_not_list_remote_configuration_as_a_blocker(self):
        self.sandbox.configure_rbe("main", siso_cache_dir=str(self.sandbox.root / "missing-cache"))
        self.assertEqual(by_name(self.plan("build", "--offline"))["readiness"]["status"], "ready")

    def test_run_plan_lists_what_will_be_stopped_and_launched(self):
        self.assertEqual(self.document("build")[0].returncode, 0)
        _, running = self.sandbox.start_app(self.sandbox.root / "elsewhere")
        document = self.plan("run")
        steps = by_name(document)
        self.assertEqual(names(document), ["environment", "select-artifact", "stop-running-instances", "launch"])
        self.assertIn(str(running.pid), steps["stop-running-instances"]["detail"])
        self.assertEqual(steps["launch"]["argv"], ["open", str(self.src / "out" / "Debug_arm64" /
                                                              "Brave Browser Development.app")])

    def test_sync_build_plan_describes_the_sync_step(self):
        steps = by_name(self.plan("sync-build"))
        self.assertEqual(steps["sync"]["cwd"], str(self.core))
        self.assertEqual(steps["sync"]["argv"][-2:], ["run", "sync"])
        self.assertTrue(steps["sync"]["writes"])
        self.assertIn("sync", steps["build"]["needs"])


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class AndroidPlanTests(AndroidTestCase):
    def setUp(self):
        super().setUp()
        self.setup_support()
        self.sandbox.configure_rbe("main")
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['android']\n")

    def plan(self, *args, **extra):
        env = self.env(FAKE_ADB_DEVICES=extra.pop("devices", "emulator-5554,device"), **extra)
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", *args, "--plan", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_build_run_android_plan_shows_preparation_gn_device_and_restart(self):
        document = self.plan("build-run", "android")
        self.assertEqual(names(document), [
            "environment", "tools", "readiness", "patch-preparation", "android-support", "gn-overrides", "build",
            "verify-output", "select-device", "install-apk", "stop-package", "launch-package"])
        steps = by_name(document)
        self.assertEqual(steps["android-support"]["status"], "planned")
        self.assertIn("applyPatches.sh", " ".join(steps["android-support"]["argv"]))
        self.assertTrue(any(path.endswith("third_party/jdk/current") for path in steps["android-support"]["writes"]))
        self.assertEqual(steps["gn-overrides"]["writes"], [str(self.src / "out" / "android_Debug_arm64" / "args.gn")])
        self.assertEqual(steps["select-device"]["status"], "resolved")
        self.assertIn("emulator-5554", steps["select-device"]["detail"])
        self.assertEqual(steps["install-apk"]["argv"][:4], ["adb", "-s", "emulator-5554", "install"])
        self.assertEqual(steps["stop-package"]["argv"][-2:], ["force-stop", "<package from the built APK>"])
        self.assertIn("kept", steps["install-apk"]["summary"])

    def test_executed_android_steps_carry_the_planned_descriptions(self):
        planned = by_name(self.plan("build-run", "android"))
        env = self.env(FAKE_ADB_DEVICES="emulator-5554,device")
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "build-run", "android",
                                   env=env)
        document = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.sandbox.config.parent / ".bdev" / "operations"
        record = json.loads((directory / (document["operation_id"] + ".json")).read_text())
        executed = {step["name"]: step for step in record["steps"]}
        for name in ("gn-overrides", "install-apk"):
            for field in ("writes", "argv", "summary", "on_failure"):
                self.assertEqual(executed[name][field], planned[name][field], (name, field))
        self.assertEqual(executed["android-support-refresh"]["writes"], planned["android-support"]["writes"])
        self.assertEqual(executed["stop-package"]["argv"][-2:], ["force-stop", "com.brave.browser_default"])

    def test_an_ambiguous_device_is_an_unresolved_step_not_a_failure(self):
        document = self.plan("build-run", "android", devices="emulator-5554,device;R58M1234,device")
        step = by_name(document)["select-device"]
        self.assertEqual(step["status"], "unresolved")
        self.assertIn("--device", step["detail"])

    def test_support_conflicts_and_missing_support_are_reported(self):
        target = self.src / "base" / "support_target.cc"
        target.parent.mkdir(exist_ok=True)
        target.write_text("upstream\n")
        self.sandbox.commit_all("main")
        target.write_text("my experiment\n")
        step = by_name(self.plan("build", "android"))["android-support"]
        self.assertEqual(step["status"], "blocked")
        self.assertIn("base/support_target.cc", step["detail"])


if __name__ == "__main__":
    unittest.main()
