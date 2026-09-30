# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android build, support dependencies, devices, and install/restart with fakes."""

import json
import subprocess
import unittest
from pathlib import Path

from tests.android_fixtures import GIT, install_fake_adb, make_support_repo
from tests.integration.test_build import SKIP, BuildTestCase

ANDROID_HOOK = """
if "build" not in argv:
    raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
import zipfile
core = os.environ["BRAVE_CORE_DIR"]
src = os.path.dirname(core)
build_dir = "android_Debug_arm64"
for index, arg in enumerate(argv):
    if arg == "-C":
        build_dir = argv[index + 1]
out = build_dir if os.path.isabs(build_dir) else os.path.join(src, "out", build_dir)
if not os.environ.get("FAKE_NO_APP"):
    os.makedirs(os.path.join(out, "apks"), exist_ok=True)
    path = os.path.join(out, "apks", "BraveMonoarm64.apk")
    if os.environ.get("FAKE_BAD_APK"):
        open(path, "w").write("not a zip")
    else:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AndroidManifest.xml", "manifest")
"""

DEVICES_TWO = "emulator-5554,device;R58M1234,device"


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class AndroidTestCase(BuildTestCase):
    def setUp(self):
        super().setUp()
        (self.src / "chrome").mkdir(exist_ok=True)
        (self.src / "chrome" / "VERSION").write_text("MAJOR=155\nMINOR=0\n")
        (self.src.parent / ".gclient").write_text("target_os = ['android']\n")
        self.sandbox.commit_all("main")
        self.hook = self.sandbox.hook(ANDROID_HOOK)
        install_fake_adb(self.sandbox)
        self.support = make_support_repo(self.sandbox.root, {"v154": 154, "v155": 155})

    def setup_support(self, ref="v155", source=None, checkout="main"):
        return self.sandbox.bdev("--json", "--config", self.config, "--checkout", checkout, "android", "setup",
                                 "--source", str(source or self.support), "--ref", ref, env=self.env())

    def wc(self, name="main"):
        return self.sandbox.checkouts[name].parent.parent / "brave-android-mac-support"

    def head(self, path):
        return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()

    def adb_calls(self):
        return [r["argv"] for r in self.sandbox.records() if r["tool"] == "adb"]


class AndroidBuildTests(AndroidTestCase):
    def test_build_needs_a_support_working_copy_and_says_how_to_create_it(self):
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEPENDENCY_INCOMPATIBLE"))
        self.assertEqual(document["error"]["repairs"][0]["argv"][:3], ["bdev", "android", "setup"])
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [])

    def test_build_prepares_support_once_then_compiles_and_verifies_the_apk(self):
        self.assertEqual(self.setup_support().returncode, 0)
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.build_argv()
        for expected in ("--target_os=android", "--target_arch=arm64", "--target_android_output_format=apk",
                         "--gn=is_component_build:false", "--gn=use_mold:false", "--use_remoteexec=true", "Debug"):
            self.assertIn(expected, argv)
        self.assertEqual(argv[argv.index("-C") + 1], "android_Debug_arm64")
        apk = self.src / "out" / "android_Debug_arm64" / "apks" / "BraveMonoarm64.apk"
        self.assertEqual(document["artifacts"][0]["path"], str(apk))
        self.assertTrue((self.src / "SUPPORT_PATCHED").exists(), "support patches were applied")
        self.assertTrue((self.src / "third_party" / "jdk" / "current" / "release").exists())
        self.assertIn("--force_gn_gen", argv, "a refreshed support state regenerates GN")
        args_gn = (self.src / "out" / "android_Debug_arm64" / "args.gn").read_text()
        self.assertIn("is_component_build=false", args_gn)
        self.assertIn("# BEGIN scaffold Android-on-Mac overrides", args_gn)
        result, document = self.document("build", "android")
        self.assertNotIn("--force_gn_gen", self.build_argv(), "current support is not refreshed again")
        self.assertEqual(args_gn, (self.src / "out" / "android_Debug_arm64" / "args.gn").read_text())

    def test_installing_never_touches_the_device(self):
        self.setup_support()
        self.document("build", "android")
        self.assertEqual(self.adb_calls(), [])

    def test_local_edits_to_files_support_patches_touch_stop_the_refresh(self):
        self.setup_support()
        target = self.src / "base" / "support_target.cc"
        target.parent.mkdir(exist_ok=True)
        target.write_text("upstream\n")
        self.sandbox.commit_all("main")
        target.write_text("my experiment\n")
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertEqual(target.read_text(), "my experiment\n")
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [])

    def test_forwarded_options_select_the_android_output(self):
        self.setup_support()
        result, document = self.document("build", "android", "-C", "Custom")
        self.assertEqual(document["artifacts"][0]["output_dir"], str(self.src / "out" / "Custom"))
        self.assertEqual(self.build_argv().count("-C"), 1)
        result, document = self.document("build", "android", "--target_os=mac")
        self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT")
        result, document = self.document("build", "android", "--gn=use_mold:true")
        argv = self.build_argv()
        self.assertIn("--gn=use_mold:true", argv)
        self.assertNotIn("--gn=use_mold:false", argv, "a forwarded value replaces the generated one")

    def test_apk_outcomes(self):
        self.setup_support()
        result, document = self.document("build", "android", env=self.env(FAKE_NO_APP="1"))
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_MISSING"))
        result, document = self.document("build", "android", env=self.env(FAKE_BAD_APK="1"))
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISMATCH")
        result, document = self.document("build", "android", "--target_android_output_format=aab")
        self.assertEqual((result.returncode, document["warnings"][0]["code"]), (0, "ARTIFACT_UNRESOLVED"))
        self.assertEqual(document["artifacts"], [])

    def test_sync_builds_the_target_union_from_the_existing_checkout_settings(self):
        (self.src.parent / ".gclient").write_text("target_os = ['ios']\n")
        result, document = self.document("sync", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", "--target_os=android,ios"])
        result, document = self.document("sync", "android", "--target_os=android")
        self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT")

    def test_checkout_without_an_android_target_is_blocked_with_the_sync_repair(self):
        self.setup_support()
        (self.src.parent / ".gclient").write_text("target_os = []\n")
        result, document = self.document("build", "android")
        self.assertEqual(document["error"]["code"], "READINESS_BLOCKED")
        repairs = [step["argv"][:3] for step in document["error"]["repairs"]]
        self.assertIn(["bdev", "sync", "android"], repairs)


class SupportPatchedFileTests(AndroidTestCase):
    def build_android(self):
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "build", "android",
                                   env=self.env())
        return result, json.loads(result.stdout)

    def test_files_changed_by_support_preparation_are_expected_but_later_edits_are_not(self):
        self.assertEqual(self.setup_support("v155").returncode, 0)
        result, document = self.build_android()
        self.assertEqual(document["status"], "ok", result.stderr)
        self.assertIn("support edit", (self.src / "base" / "BUILD.gn").read_text())
        plan = json.loads(self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "build",
                                            "android", "--plan", env=self.env()).stdout)
        steps = {step["name"]: step for step in plan["data"]["plan"]["steps"]}
        self.assertEqual(steps["patch-preparation"]["status"], "current")
        result, document = self.build_android()
        self.assertEqual(document["status"], "ok", "the second build does not treat support changes as local edits")
        with open(self.src / "base" / "BUILD.gn", "a") as stream:
            stream.write("my own experiment\n")
        result, document = self.build_android()
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")


class SupportWorkingCopyTests(AndroidTestCase):
    def test_a_shallow_local_source_still_produces_an_isolated_working_copy(self):
        shallow = self.sandbox.root / "shallow-support"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(self.support), str(shallow)], check=True)
        result = self.setup_support("v155", source=shallow)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(self.wc()), self.head(self.support))
        self.assertTrue(subprocess.run(["git", "-C", str(self.wc()), "config", "lfs.storage"], capture_output=True,
                                       text=True).stdout.strip().endswith("android-support-lfs"))

    def test_two_checkouts_use_different_revisions_without_sharing_a_working_copy(self):
        other = self.sandbox.make_checkout("other", git=True)
        (other.parent / "chrome").mkdir()
        (other.parent / "chrome" / "VERSION").write_text("MAJOR=154\n")
        (other.parent.parent / ".gclient").write_text("target_os = ['android']\n")
        self.sandbox.add_patch("other", "base/BUILD.gn")
        self.sandbox.commit_all("other")
        self.sandbox.write_config([("main", self.core, "environments/main"), ("other", other, "environments/other")])
        for name in ("main", "other"):
            self.sandbox.bdev("env", "init", "--checkout", name, "--config", self.config)
            self.sandbox.approve(name)
        self.assertEqual(self.setup_support("v155", checkout="main").returncode, 0)
        main_head = self.head(self.wc("main"))
        self.assertEqual(self.setup_support("v154", checkout="other").returncode, 0)
        self.assertEqual(self.head(self.wc("main")), main_head, "the first working copy was not switched")
        self.assertNotEqual(self.head(self.wc("main")), self.head(self.wc("other")))
        self.assertEqual(sorted(item.name for item in (Path(self.config).parent / ".bdev" / "cache").iterdir()),
                         ["android-support-lfs", "android-support.git"], "one shared object cache and large-file store")
        result = self.sandbox.bdev("--json", "--config", self.config, "build", "android", "--checkout", "other",
                                   env=self.env())
        self.assertEqual(json.loads(result.stdout)["status"], "ok", result.stderr)
        self.assertEqual(self.head(self.wc("main")), main_head)

    def test_incompatible_revision_is_reported_with_the_gate_reason_and_repairs(self):
        self.setup_support("v154")
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEPENDENCY_INCOMPATIBLE"))
        self.assertIn("supports Chromium 154", document["error"]["details"]["reason"])
        self.assertTrue(document["error"]["repairs"])
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [])
        self.assertFalse((self.src / "SUPPORT_PATCHED").exists())

    def test_dirty_files_unpushed_commits_and_branches_are_preserved(self):
        self.setup_support("v155")
        wc = self.wc()
        subprocess.run([*GIT, "-C", str(wc), "checkout", "-q", "-b", "my-work"], check=True)
        (wc / "notes.txt").write_text("committed locally\n")
        subprocess.run([*GIT, "-C", str(wc), "add", "notes.txt"], check=True)
        subprocess.run([*GIT, "-C", str(wc), "commit", "-q", "-m", "local"], check=True)
        (wc / "patches" / "marker").write_text("edited by developer\n")
        head = self.head(wc)
        result, document = self.setup_support("v154"), None
        document = json.loads(result.stdout)
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertEqual(self.head(wc), head)
        self.assertEqual((wc / "patches" / "marker").read_text(), "edited by developer\n")
        branch = subprocess.run(["git", "-C", str(wc), "branch", "--show-current"], capture_output=True, text=True)
        self.assertEqual(branch.stdout.strip(), "my-work")
        # Without --ref the existing working copy is used as it is.
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "android", "setup",
                                   "--source", str(self.support), env=self.env())
        self.assertEqual(json.loads(result.stdout)["status"], "ok")
        self.assertEqual(self.head(wc), head)

    def test_a_directory_that_is_not_a_working_copy_is_never_replaced(self):
        self.wc().mkdir()
        (self.wc() / "keep.txt").write_text("mine\n")
        result = self.setup_support()
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "OWNERSHIP_CONFLICT")
        self.assertTrue((self.wc() / "keep.txt").exists())

    def test_dirty_support_inputs_are_applied_as_the_developer_left_them(self):
        self.setup_support("v155")
        (self.wc() / "patches" / "marker").write_text("developer variant\n")
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.src / "SUPPORT_PATCHED").read_text(), "developer variant\n")
        self.assertEqual((self.wc() / "patches" / "marker").read_text(), "developer variant\n")


class DeviceTests(AndroidTestCase):
    def setUp(self):
        super().setUp()
        self.setup_support()
        self.document("build", "android")
        self.sandbox.record.unlink()

    def run_android(self, *args, devices=DEVICES_TWO, command="run", **extra):
        env = self.env(FAKE_ADB_DEVICES=devices, **extra)
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", command, *args, env=env)
        return result, json.loads(result.stdout)

    def test_several_usable_devices_require_a_choice(self):
        result, document = self.run_android("android")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "DEVICE_AMBIGUOUS"))
        self.assertEqual(document["error"]["details"]["devices"], ["emulator-5554 (device)", "R58M1234 (device)"])
        self.assertIn("--device", document["error"]["details"]["example"])
        self.assertFalse([c for c in self.adb_calls() if "install" in c])

    def test_choosing_a_device_installs_over_the_app_and_restarts_only_that_package(self):
        result, document = self.run_android("android", "--device", "R58M1234")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.adb_calls()
        self.assertTrue(all(c[:2] == ["-s", "R58M1234"] for c in calls if c != ["devices"]))
        install = next(c for c in calls if "install" in c)
        self.assertIn("-r", install)
        self.assertTrue(install[-1].endswith("BraveMonoarm64.apk"))
        self.assertIn(["-s", "R58M1234", "shell", "am", "force-stop", "com.brave.browser_default"], calls)
        joined = " ".join(" ".join(c) for c in calls)
        for destructive in ("uninstall", "pm clear", "pm uninstall"):
            self.assertNotIn(destructive, joined, "restart keeps profiles and app data")
        self.assertEqual(document["data"]["run"]["package"], "com.brave.browser_default")

    def test_configured_default_device_resolves_the_choice_and_a_flag_overrides_it(self):
        self.sandbox.config.write_text(self.sandbox.config.read_text() + '\n[defaults]\nandroid_device = "emulator-5554"\n')
        result, document = self.run_android("android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["data"]["run"]["device"], "emulator-5554")
        result, document = self.run_android("android", "--device", "R58M1234")
        self.assertEqual(document["data"]["run"]["device"], "R58M1234")

    def test_offline_unauthorized_and_missing_devices_get_targeted_guidance(self):
        for devices, expected in (("R58M1234,offline", "offline"), ("R58M1234,unauthorized", "USB debugging"),
                                  ("", "No usable")):
            with self.subTest(devices=devices):
                result, document = self.run_android("android", devices=devices)
                self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEVICE_UNAVAILABLE"))
                self.assertIn(expected, json.dumps(document["error"]))
        result, document = self.run_android("android", "--device", "R58M1234", devices="R58M1234,unauthorized")
        self.assertEqual(document["error"]["code"], "DEVICE_UNAVAILABLE")
        self.assertIn("USB debugging", document["error"]["details"]["recovery"])
        result, document = self.run_android("android", "--device", "nope")
        self.assertEqual(document["error"]["code"], "DEVICE_UNAVAILABLE")

    def test_one_usable_device_among_unusable_ones_is_used(self):
        result, document = self.run_android("android", devices="R58M1234,offline;emulator-5554,device")
        self.assertEqual(document["data"]["run"]["device"], "emulator-5554")

    def test_deploy_is_the_same_as_run_android(self):
        result, document = self.run_android("android", "--device", "emulator-5554", command="deploy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["command"], "deploy")
        self.assertTrue([c for c in self.adb_calls() if "install" in c])
        result, document = self.run_android("mac", command="deploy")
        self.assertEqual(document["error"]["code"], "INVALID_INPUT")

    def test_missing_apk_and_failed_install_or_launch(self):
        apk = self.src / "out" / "android_Debug_arm64" / "apks" / "BraveMonoarm64.apk"
        result, document = self.run_android("android", "--device", "emulator-5554", FAKE_ADB_INSTALL_FAIL="1")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "LAUNCH_FAILED"))
        apk.unlink()
        result, document = self.run_android("android", "--device", "emulator-5554")
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISSING")

    def test_freshness_is_reported_for_the_installed_build(self):
        result, document = self.run_android("android", "--device", "emulator-5554")
        self.assertEqual(document["data"]["run"]["freshness"]["status"], "current")
        apk = self.src / "out" / "android_Debug_arm64" / "apks" / "BraveMonoarm64.apk"
        result, document = self.run_android("android", "--device", "emulator-5554", "--artifact", str(apk))
        self.assertEqual(result.returncode, 0)

    def test_build_run_chooses_the_device_before_building(self):
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.run_android("android", command="build-run")
        self.assertEqual(document["error"]["code"], "DEVICE_AMBIGUOUS")
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [])
        result, document = self.run_android("android", "--device", "emulator-5554", command="build-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["data"]["run"]["device"], "emulator-5554")

    def test_unresolved_output_stops_build_run_before_the_device_is_touched(self):
        result, document = self.run_android("android", "--device", "emulator-5554", "--target", "brave_unit_tests",
                                            command="build-run")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_UNRESOLVED"))
        self.assertFalse([c for c in self.adb_calls() if "install" in c])


class AndroidDoctorTests(AndroidTestCase):
    def doctor(self, *args, path=None):
        env = self.env()
        if path:
            env["PATH"] = path
        result = self.sandbox.bdev("--json", "--config", self.config, "doctor", *args, env=env)
        return result, json.loads(result.stdout)

    def test_a_missing_adb_does_not_block_macos_readiness(self):
        (self.sandbox.bin / "adb").unlink()
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"))
        result, document = self.doctor("android", "--checkout", "main")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
        statuses = {c["name"]: c["status"] for c in document["checks"]}
        self.assertEqual(statuses["adb"], "blocker")
        self.assertEqual(statuses["android-support-working-copy"], "blocker")

    def test_ready_android_scope_with_a_compatible_support_revision(self):
        self.setup_support()
        result, document = self.doctor("android", "--checkout", "main")
        self.assertEqual(result.returncode, 0, document.get("error"))
        statuses = {c["name"]: c["status"] for c in document["checks"]}
        self.assertEqual(statuses["android-support-compatibility"], "pass")
        self.assertEqual(statuses["android-support-currency"], "warning", "the first build will refresh support")
        self.assertFalse((self.src / "SUPPORT_PATCHED").exists(), "doctor changes nothing")

    def test_incompatible_support_blocks_the_android_scope_only(self):
        self.setup_support("v154")
        result, document = self.doctor("android", "--checkout", "main")
        self.assertEqual(document["error"]["code"], "READINESS_BLOCKED")
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
