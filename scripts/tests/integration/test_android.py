# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android build, support dependencies, devices, and install/restart with fakes."""

import json
import os
import pty
import subprocess
import unittest
from pathlib import Path

from tests.android_fixtures import GIT, install_fake_aapt2, install_fake_adb, make_support_repo, script_contracts
from tests.integration.test_build import SKIP, BuildTestCase
from tests.schema_validation import Validator

ANDROID_HOOK = """
if "sync" in argv and not os.environ.get("FAKE_SYNC_KEEPS_TARGETS"):
    requested = [a.split("=", 1)[1] for a in argv if a.startswith("--target_os=")]
    gclient = os.path.join(os.path.dirname(os.environ["BRAVE_CORE_DIR"]), "..", ".gclient")
    with open(gclient, "a") as stream:
        stream.write("target_os = %r\\n" % (requested[0].split(",") if requested else [],))
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
os.makedirs(out, exist_ok=True)
generated = {"use_remoteexec": "true" if "--use_remoteexec=true" in argv else "false"}
for index, arg in enumerate(argv):
    if arg.startswith("--gn=") or arg == "--gn":
        key, _, value = (arg[5:] if arg != "--gn" else argv[index + 1]).partition(":")
        generated[key] = value
    elif arg.startswith("--use_remoteexec="):
        generated["use_remoteexec"] = arg.partition("=")[2]
with open(os.path.join(out, "args_generated.gni"), "w") as stream:
    stream.write("".join("%s=%s\\n" % item for item in generated.items()))
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
    support_overlay = False

    def setUp(self):
        super().setUp()
        self.sandbox.install_script_contracts(script_contracts())
        (self.src / "chrome").mkdir(exist_ok=True)
        (self.src / "chrome" / "VERSION").write_text("MAJOR=155\nMINOR=0\n")
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['android']\n")
        install_fake_aapt2(self.src)
        self.sandbox.commit_all("main")
        self.hook = self.sandbox.hook(ANDROID_HOOK)
        install_fake_adb(self.sandbox)
        self.support = make_support_repo(self.sandbox.root, {"v154": 154, "v155": 155}, overlay=self.support_overlay)

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


def effective_gn_args(output_dir):
    """What GN would see: each import is expanded in place and the last assignment of a key wins."""
    final = {}

    def read(path):
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith('import("//'):
                read(Path(output_dir).parents[1] / line[len('import("//'):-2])
            elif "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                final[key.strip()] = value.strip()

    read(Path(output_dir) / "args.gn")
    return final


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

    def test_skip_refresh_keeps_local_edits_to_support_patch_targets(self):
        self.setup_support()
        target = self.src / "base" / "support_target.cc"
        target.parent.mkdir(exist_ok=True)
        target.write_text("upstream\n")
        self.sandbox.commit_all("main")
        target.write_text("my experiment\n")
        result, document = self.document("build", "android", "--skip-support-refresh")
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

    def final_gn_args(self, *args, output="android_Debug_arm64"):
        result, document = self.document("build", "android", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return effective_gn_args(self.src / "out" / output)

    def test_final_gn_settings_default_to_the_scaffold_choices(self):
        self.setup_support()
        final = self.final_gn_args()
        self.assertEqual({key: final[key] for key in ("is_component_build", "enable_android_secondary_abi",
                                                       "use_mold", "use_remoteexec", "android_static_analysis")},
                         {"is_component_build": "false", "enable_android_secondary_abi": "false", "use_mold": "false",
                          "use_remoteexec": "true", "android_static_analysis": '"off"'})

    def test_accepted_gn_options_reach_the_final_settings(self):
        self.setup_support()
        final = self.final_gn_args("--gn=use_mold:true", "--gn", 'android_static_analysis:"build_server"',
                                   "--gn=is_component_build:true")
        self.assertEqual((final["use_mold"], final["android_static_analysis"], final["is_component_build"]),
                         ("true", '"build_server"', "true"))
        self.assertEqual(final["enable_android_secondary_abi"], "false", "unrelated choices are kept")
        final = self.final_gn_args("--use_remoteexec=false")
        self.assertEqual((final["use_remoteexec"], final["use_mold"]), ("false", "false"),
                         "an override applies to one build; the next build starts from the defaults again")

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

    def test_ninja_directory_build_does_not_install_an_existing_default_apk(self):
        self.assertEqual(self.setup_support().returncode, 0)
        self.assertEqual(self.document("build", "android")[0].returncode, 0)
        alternate = self.src / "out/Alternate"
        self.hook = self.sandbox.hook(ANDROID_HOOK.replace(
            'out = build_dir if os.path.isabs(build_dir) else os.path.join(src, "out", build_dir)',
            'out = os.environ["FAKE_NINJA_OUTPUT"]'))
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("build-run", "android", "--device", "emulator-5554",
                                         "--ninja=C:" + str(alternate),
                                         env=self.env(FAKE_NINJA_OUTPUT=str(alternate), FAKE_ADB_DEVICES="emulator-5554,device"))
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(document["error"]["code"], "ARTIFACT_UNRESOLVED")
        self.assertTrue((alternate / "apks/BraveMonoarm64.apk").exists())
        self.assertFalse(any("install" in argv or "monkey" in argv for argv in self.adb_calls()))


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
    def test_a_shallow_local_source_produces_a_shared_checkout(self):
        shallow = self.sandbox.root / "shallow-support"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(self.support), str(shallow)], check=True)
        result = self.setup_support("v155", source=shallow)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(self.wc()), self.head(self.support))
        self.assertTrue(subprocess.run(["git", "-C", str(self.wc()), "config", "lfs.storage"], capture_output=True,
                                       text=True).stdout.strip().endswith("brave-android-mac-support/.git/lfs"))

    def test_two_checkouts_link_to_one_shared_revision(self):
        other = self.sandbox.make_checkout("other", git=True)
        (other.parent / "chrome").mkdir()
        (other.parent / "chrome" / "VERSION").write_text("MAJOR=155\n")
        self.sandbox.write_config([("main", self.core, "environments/main"), ("other", other, "environments/other")])
        self.assertEqual(self.setup_support("v155", checkout="main").returncode, 0)
        self.assertEqual(self.setup_support("v155", checkout="other").returncode, 0)
        self.assertTrue(self.wc("main").is_symlink())
        self.assertEqual(self.wc("main").resolve(), self.wc("other").resolve())
        self.assertFalse((Path(self.config).parent / ".bdev/cache/android-support.git").exists())
        result = self.setup_support("v154", checkout="other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(self.wc("main")), self.head(self.wc("other")))
        self.assertEqual((self.wc("main") / "SUPPORTS_CHROMIUM").read_text().strip(), "154")

    def test_setup_accepts_a_commit_id(self):
        ref = self.head(self.support)
        result = self.setup_support(ref)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(self.wc()), ref)

    def test_wrong_workspace_link_is_reported_without_building(self):
        self.assertEqual(self.setup_support().returncode, 0)
        self.wc().unlink()
        self.wc().symlink_to(self.support)
        result, document = self.document("build", "android")
        self.assertEqual(document["error"]["code"], "DEPENDENCY_INCOMPATIBLE")
        self.assertEqual(document["error"]["repairs"][0]["argv"][:3], ["bdev", "android", "setup"])
        self.assertFalse(any("build" in record["argv"] for record in self.node_calls()))

    def test_custom_shared_location_is_relative_to_configuration(self):
        path = Path(self.config)
        path.write_text('android_support_path = "dependencies/android"\n' + path.read_text())
        result = self.setup_support()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wc().resolve(), path.parent / "dependencies/android")

    def test_existing_workspace_copy_is_adopted_without_losing_local_files(self):
        subprocess.run(["git", "clone", "-q", str(self.support), str(self.wc())], check=True)
        (self.wc() / "local-notes").write_text("keep me")
        result = self.setup_support("v155")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.wc().is_symlink())
        self.assertEqual((self.wc() / "local-notes").read_text(), "keep me")

    def test_existing_second_copy_is_preserved_before_linking(self):
        self.assertEqual(self.setup_support().returncode, 0)
        shared = self.wc().resolve()
        self.wc().unlink()
        subprocess.run(["git", "clone", "-q", str(self.support), str(self.wc())], check=True)
        (self.wc() / "local-notes").write_text("keep me")
        result = self.setup_support()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wc().resolve(), shared)
        self.assertEqual((self.wc().with_name("brave-android-mac-support.previous") / "local-notes").read_text(), "keep me")

    def test_legacy_directory_of_links_is_preserved_before_linking(self):
        self.wc().mkdir()
        for name in ("copyMacRes.sh", "applyPatches.sh", "res"):
            (self.wc() / name).symlink_to(self.support / name)
        result = self.setup_support()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.wc().is_symlink())
        backup = self.wc().with_name("brave-android-mac-support.previous")
        self.assertEqual((backup / "copyMacRes.sh").readlink(), self.support / "copyMacRes.sh")

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
        document = json.loads(result.stdout)
        self.assertEqual(Validator().problems(document), [], result.stdout)
        return result, document

    def test_several_usable_devices_require_a_choice(self):
        result, document = self.run_android("android")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "DEVICE_AMBIGUOUS"))
        self.assertEqual(document["error"]["details"]["devices"], ["emulator-5554 (device)", "R58M1234 (device)"])
        self.assertIn("--device", document["error"]["details"]["example"])
        self.assertFalse([c for c in self.adb_calls() if "install" in c])

    def test_terminal_picker_names_devices_and_remembers_the_choice(self):
        stdout, terminal = self.terminal_run(b'2\ny\n')
        self.assertIn('on R58M1234', stdout)
        self.assertIn('Pixel_API_35 (emulator) - emulator-5554', terminal)
        self.assertIn('Pixel (physical device) - R58M1234', terminal)
        from scaffold.common.config import load_config
        self.assertEqual(load_config(self.config).default_android_device, 'R58M1234')

    def test_terminal_picker_enter_uses_first_and_all_deploys_to_both(self):
        original = Path(self.config).read_bytes()
        stdout, terminal = self.terminal_run(b'\nn\n')
        self.assertIn('on emulator-5554', stdout)
        self.assertIn('Enter for 1', terminal)
        self.assertEqual([call[1] for call in self.adb_calls() if 'install' in call], ['emulator-5554'])
        self.sandbox.record.unlink()
        stdout, terminal = self.terminal_run(b'a\n')
        self.assertIn('a. All compatible devices', terminal)
        self.assertNotIn('Remember', terminal)
        self.assertEqual([call[1] for call in self.adb_calls() if 'install' in call], ['emulator-5554', 'R58M1234'])
        self.assertIn('R58M1234: installed and restarted', stdout)
        self.assertEqual(Path(self.config).read_bytes(), original)

    def terminal_run(self, answers):
        master, slave = pty.openpty()
        process = None
        try:
            process = subprocess.Popen([str(self.sandbox.scripts / 'bdev'), '--config', str(self.config),
                                        '--checkout', 'main', 'run', 'android'], cwd=self.sandbox.root,
                                       env=self.env(FAKE_ADB_DEVICES=DEVICES_TWO),
                                       stdin=slave, stderr=slave, stdout=subprocess.PIPE, text=True)
            os.write(master, answers)
            stdout, _ = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout)
            os.set_blocking(master, False)
            terminal = os.read(master, 65536).decode()
            return stdout, terminal
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            os.close(master)
            os.close(slave)

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

    def test_failed_install_records_only_the_attempted_child_phase(self):
        result, document = self.run_android("android", "--device", "emulator-5554", FAKE_ADB_INSTALL_FAIL="7")
        self.assertEqual(result.returncode, 5, result.stderr)
        record = json.loads((self.sandbox.config.parent / ".bdev/operations" /
                             (document["operation_id"] + ".json")).read_text())
        phases = [step for step in record["steps"] if step["name"] in
                  ("install-apk", "stop-package", "launch-package")]
        self.assertEqual([step["name"] for step in phases], ["install-apk"])
        self.assertEqual((phases[0]["status"], phases[0]["outcome"]["exit"]), ("failed", 7))
        self.assertEqual(document["child_exit_code"], 7)
        self.assertFalse(any("force-stop" in argv or "monkey" in argv for argv in self.adb_calls()))

    def test_missing_apk_and_failed_install_or_launch(self):
        apk = self.src / "out" / "android_Debug_arm64" / "apks" / "BraveMonoarm64.apk"
        result, document = self.run_android("android", "--device", "emulator-5554", FAKE_ADB_INSTALL_FAIL="1")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "LAUNCH_FAILED"))
        apk.unlink()
        result, document = self.run_android("android", "--device", "emulator-5554")
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISSING")

    def test_run_and_deploy_do_not_inspect_sources(self):
        (self.src.parent / ".gclient_entries").write_text("invalid entries\n")
        (self.wc() / "patches/marker").write_text("edited support\n")
        for command in ("run", "deploy"):
            with self.subTest(command=command):
                result, document = self.run_android("android", "--device", "emulator-5554", command=command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("freshness", document["data"]["run"])
                self.assertFalse(any(w["code"] in ("STALE_BUILD", "UNKNOWN_FRESHNESS")
                                     for w in document["warnings"]))
                log = Path(next(line.removeprefix("Log: ") for line in result.stderr.splitlines()
                                if line.startswith("Log: "))).read_text()
                self.assertNotIn("Checking source state", log)
                self.assertNotIn("applyPatches.sh", log)

    def test_build_run_chooses_the_device_before_building(self):
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.run_android("android", command="build-run")
        self.assertEqual(document["error"]["code"], "DEVICE_AMBIGUOUS")
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [])
        result, document = self.run_android("android", "--device", "emulator-5554", command="build-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["data"]["run"]["device"], "emulator-5554")

    def test_all_devices_builds_once_and_installs_on_each_device(self):
        result, document = self.run_android('android', '--all-devices', command='build-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([call for call in self.node_calls() if 'build' in call['argv']]), 1)
        self.assertEqual([call[1] for call in self.adb_calls() if 'install' in call],
                         ['emulator-5554', 'R58M1234'])
        self.assertEqual([(item['device'], item['status']) for item in document['data']['run']['devices']],
                         [('emulator-5554', 'ok'), ('R58M1234', 'ok')])

    def test_all_devices_sync_build_run_syncs_and_builds_once(self):
        result, document = self.run_android('android', '--all-devices', command='sync-build-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['argv'][1:3] for call in self.node_calls()], [['run', 'sync'], ['run', 'build']])
        self.assertEqual(len(document['data']['run']['devices']), 2)

    def test_all_devices_plan_uses_forwarded_build_architecture(self):
        properties = {'emulator-5554': {'ro.product.cpu.abilist': 'x86_64'}}
        result, document = self.run_android('android', '--all-devices', '--target_arch=x64', '--plan',
                                            command='build-run', FAKE_ADB_PROPERTIES=json.dumps(properties))
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [step for step in document['data']['plan']['steps'] if step['name'] == 'install-apk']
        self.assertEqual([step['argv'][2] for step in installs], ['emulator-5554'])

    def test_all_devices_continues_after_failure_and_records_each_result(self):
        result, document = self.run_android('android', '--all-devices', FAKE_ADB_FAIL_DEVICE='emulator-5554')
        self.assertEqual((result.returncode, document['error']['code']), (5, 'LAUNCH_FAILED'))
        self.assertEqual(document['child_exit_code'], 7)
        self.assertEqual([(item['device'], item['status']) for item in document['data']['run']['devices']],
                         [('emulator-5554', 'error'), ('R58M1234', 'ok')])
        self.assertFalse([call for call in self.adb_calls() if call[:2] == ['-s', 'emulator-5554']
                          and 'force-stop' in call])
        record = json.loads((self.sandbox.config.parent / '.bdev/operations' /
                             (document['operation_id'] + '.json')).read_text())
        self.assertEqual(record['details']['device_results'], document['data']['run']['devices'])

    def test_all_devices_skips_incompatible_and_unavailable_devices(self):
        properties = {'emulator-5554': {'ro.product.cpu.abilist': 'x86_64'},
                      'old-phone': {'ro.build.version.sdk': '28'}}
        result, document = self.run_android('android', '--all-devices',
                                            devices=DEVICES_TWO + ';old-phone,device;locked,unauthorized',
                                            FAKE_ADB_PROPERTIES=json.dumps(properties))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[1] for call in self.adb_calls() if 'install' in call], ['R58M1234'])
        outcomes = {item['device']: item for item in document['data']['run']['devices']}
        self.assertEqual(outcomes['old-phone']['status'], 'skipped')
        self.assertIn('API 29', outcomes['old-phone']['reason'])
        self.assertEqual(outcomes['locked']['status'], 'skipped')

    def test_all_devices_stops_before_build_when_no_abi_matches(self):
        properties = {serial: {'ro.product.cpu.abilist': 'x86_64'} for serial in ('emulator-5554', 'R58M1234')}
        result, document = self.run_android('android', '--all-devices', command='build-run',
                                            FAKE_ADB_PROPERTIES=json.dumps(properties))
        self.assertEqual((result.returncode, document['error']['code']), (3, 'DEVICE_UNAVAILABLE'))
        self.assertEqual(self.node_calls(), [])
        self.assertFalse([call for call in self.adb_calls() if 'install' in call])

    def test_all_devices_unknown_apk_requirements_install_nothing(self):
        result, document = self.run_android('android', '--all-devices', FAKE_APK_MIN_SDK='unknown')
        self.assertEqual((result.returncode, document['error']['code']), (5, 'ARTIFACT_UNRESOLVED'))
        self.assertFalse([call for call in self.adb_calls() if 'install' in call])

    def test_all_devices_fails_when_actual_apk_matches_no_device(self):
        for extra in ({'FAKE_APK_MIN_SDK': '99'}, {'FAKE_APK_ABI': 'x86_64'}):
            result, document = self.run_android('android', '--all-devices', **extra)
            self.assertEqual((result.returncode, document['error']['code']), (3, 'DEVICE_UNAVAILABLE'))
            self.assertTrue(all(item['status'] == 'skipped' for item in document['data']['run']['devices']))
        self.assertFalse([call for call in self.adb_calls() if 'install' in call])

    def test_all_devices_reports_unreadable_device_properties_without_installing(self):
        properties = {serial: {'ro.product.cpu.abilist': ''} for serial in ('emulator-5554', 'R58M1234')}
        result, document = self.run_android('android', '--all-devices',
                                            FAKE_ADB_PROPERTIES=json.dumps(properties))
        self.assertEqual((result.returncode, document['error']['code']), (3, 'DEVICE_UNAVAILABLE'))
        self.assertTrue(all('Could not read' in item['reason'] for item in document['error']['details']['devices']))
        self.assertFalse([call for call in self.adb_calls() if 'install' in call])

    def test_all_devices_overrides_saved_default_and_conflicts_with_device_flag(self):
        with open(self.config, 'a') as stream:
            stream.write('\n[defaults]\nandroid_device = "disconnected"\n')
        result, document = self.run_android('android', '--all-devices', command='deploy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(document['data']['run']['devices']), 2)
        self.sandbox.record.unlink()
        result, document = self.run_android('android', '--all-devices', '--device', 'R58M1234', command='build-run')
        self.assertEqual((result.returncode, document['error']['code']), (2, 'SELECTOR_CONFLICT'))
        self.assertEqual(self.node_calls(), [])
        self.assertEqual(self.adb_calls(), [])

    def test_all_devices_plan_lists_both_without_installing(self):
        result, document = self.run_android('android', '--all-devices', '--plan', command='build-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [step for step in document['data']['plan']['steps'] if step['name'] == 'install-apk']
        self.assertEqual([step['argv'][2] for step in installs], ['emulator-5554', 'R58M1234'])
        self.assertFalse([call for call in self.adb_calls() if 'install' in call])

    def test_all_devices_rejects_other_platforms_and_build_without_run(self):
        for command, target in (('run', 'mac'), ('build-run', 'ios'), ('build', 'android'), ('sync-build', 'android')):
            result, document = self.run_android(target, '--all-devices', command=command)
            self.assertEqual((result.returncode, document['error']['code']), (2, 'INVALID_INPUT'), result.stderr)
        self.assertEqual(self.node_calls(), [])
        self.assertEqual(self.adb_calls(), [])

    def test_unresolved_output_stops_build_run_before_the_device_is_touched(self):
        result, document = self.run_android("android", "--device", "emulator-5554", "--target", "brave_unit_tests",
                                            command="build-run")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_UNRESOLVED"))
        self.assertFalse([c for c in self.adb_calls() if "install" in c])


class PhaseReadinessTests(AndroidTestCase):
    """Each phase is checked for what it needs; sync establishes the Android target the build then requires."""

    def setUp(self):
        super().setUp()
        self.setup_support()
        self.sandbox.configure_rbe("main")
        (self.src.parent / ".gclient").write_text((self.src.parent / ".gclient").read_text() + "target_os = []\n")

    def combined(self, **extra):
        return self.document("sync-build", "android", env=self.env(**extra))

    def test_sync_is_not_blocked_by_the_target_it_is_about_to_establish(self):
        result, document = self.combined()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [call["argv"][1:3] for call in self.node_calls()]
        self.assertEqual(calls, [["run", "sync"], ["run", "build"]])

    def test_build_readiness_is_checked_again_after_sync(self):
        result, document = self.combined(FAKE_SYNC_KEEPS_TARGETS="1")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
        self.assertEqual([call["argv"][1:3] for call in self.node_calls()], [["run", "sync"]])
        self.assertIn("android-gclient-target", {c["name"] for c in document["error"]["details"]["checks"]})

    def test_a_standalone_build_still_needs_the_target_first(self):
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
        self.assertEqual(self.node_calls(), [])

    def test_a_remote_android_build_needs_local_rbe_configuration_and_an_offline_one_does_not(self):
        self.combined()
        self.sandbox.configure_rbe("main", siso_cache_dir=str(self.sandbox.root / "no-such-cache"))
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['android']\n")
        result, document = self.document("build", "android")
        self.assertEqual(document["error"]["code"], "READINESS_BLOCKED")
        result, document = self.document("build", "android", "--offline")
        self.assertEqual(result.returncode, 0, result.stderr)


class PackageIdentityTests(AndroidTestCase):
    """The package to stop and launch comes from the APK itself, never from a default."""

    def setUp(self):
        super().setUp()
        self.setup_support()
        self.aapt2 = self.src / "third_party" / "android_build_tools" / "aapt2" / "cipd" / "aapt2"

    def combined(self, command="build-run", **extra):
        self.sandbox.record.unlink(missing_ok=True)
        env = self.env(FAKE_ADB_DEVICES="emulator-5554,device", **extra)
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", command, "android",
                                   "--device", "emulator-5554", env=env)
        return result, json.loads(result.stdout)

    def device_changes(self):
        return [call for call in self.adb_calls() if any(word in call for word in ("install", "force-stop", "monkey"))]

    def test_a_non_default_package_is_the_one_stopped_and_launched(self):
        result, document = self.combined(FAKE_APK_PACKAGE="com.brave.browser_beta")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.adb_calls()
        self.assertIn(["-s", "emulator-5554", "shell", "am", "force-stop", "com.brave.browser_beta"], calls)
        self.assertIn(["-s", "emulator-5554", "shell", "monkey", "-p", "com.brave.browser_beta", "1"], calls)
        self.assertNotIn("com.brave.browser_default", " ".join(" ".join(call) for call in calls))
        self.assertEqual(document["data"]["run"]["package"], "com.brave.browser_beta")
        self.assertTrue(document["artifacts"][0]["package_verified"])

    def test_an_apk_for_an_unrelated_package_never_reaches_the_device(self):
        result, document = self.combined(FAKE_APK_PACKAGE="org.example.unrelated")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_MISMATCH"))
        self.assertEqual(self.device_changes(), [])

    def test_unproven_identity_blocks_combined_commands_and_leaves_standalone_build_a_warning(self):
        for label, change in (("no inspector", lambda: self.aapt2.unlink()), ("failing inspector", lambda: None)):
            with self.subTest(label):
                if label == "no inspector":
                    change()
                extra = {"FAKE_AAPT2_FAIL": "1"} if label == "failing inspector" else {}
                result, document = self.combined(**extra)
                self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]),
                                 (5, "ARTIFACT_UNRESOLVED", 0))
                self.assertIn("package", document["error"]["message"])
                self.assertEqual(self.device_changes(), [])
                result, document = self.combined("build", **extra)
                self.assertEqual((result.returncode, document["warnings"][0]["code"], document["artifacts"]),
                                 (0, "ARTIFACT_UNRESOLVED", []))

    def test_running_an_existing_apk_needs_a_proven_package_too(self):
        self.assertEqual(self.combined()[0].returncode, 0)
        self.aapt2.unlink()
        result, document = self.combined("run")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(self.device_changes(), [])
        commands = [step["argv"][:3] for step in document["error"]["repairs"]]
        self.assertNotIn(["bdev", "tools", "setup"], commands, "tools setup cannot provide aapt2")
        self.assertIn(["bdev", "build", "android"], commands, "support preparation copies aapt2")

    def test_a_failed_stop_is_not_reported_as_a_restart(self):
        result, document = self.combined(FAKE_ADB_FORCE_STOP_FAIL="1")
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "LAUNCH_FAILED"))
        self.assertIn("stop", document["error"]["message"].lower())
        self.assertFalse([call for call in self.adb_calls() if "monkey" in call])
        record = json.loads((self.sandbox.config.parent / ".bdev/operations" /
                             (document["operation_id"] + ".json")).read_text())
        phases = {step["name"]: step for step in record["steps"]}
        self.assertEqual(phases["install-apk"]["outcome"]["exit"], 0)
        self.assertEqual((phases["stop-package"]["status"], phases["stop-package"]["outcome"]["exit"]),
                         ("failed", 1))
        self.assertNotIn("launch-package", phases)


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
