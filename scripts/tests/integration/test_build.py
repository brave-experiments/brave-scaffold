# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Build, output selection, argument forwarding, and artifact outcomes with a fake package command."""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from tests.support import SandboxTest, write_executable

BUILD_HOOK = """
if "build" not in argv:
    raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
core = os.environ["BRAVE_CORE_DIR"]
src = os.path.dirname(core)
build_dir = None
for index, arg in enumerate(argv):
    if arg == "-C":
        build_dir = argv[index + 1]
    elif arg.startswith("-C") and not arg.startswith("--"):
        build_dir = arg[2:]
    elif arg.startswith("--target_arch="):
        arch = arg.split("=", 1)[1]
if build_dir is None:
    build_dir = "Debug_arm64"
if os.environ.get("FAKE_NO_APP"):
    pass
else:
    out = build_dir if os.path.isabs(build_dir) else os.path.join(src, "out", build_dir)
    make_app(out)
# Decoys in places a wrong interpretation would choose.
if os.environ.get("FAKE_DECOYS") and build_dir not in ("Debug_arm64",):
    make_app(os.path.join(core, build_dir), bundle_id="com.brave.ScaffoldTest.decoy")
    make_app(os.path.join(src, "out", "Debug_arm64"), bundle_id="com.brave.ScaffoldTest.decoy")
"""

SKIP = not (shutil.which("direnv") and sys.platform == "darwin")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class BuildTestCase(SandboxTest):
    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout("main", git=True)
        self.src = self.core.parent
        self.sandbox.prepare_environment("main")
        self.sandbox.add_patch("main", "base/BUILD.gn")
        self.sandbox.configure_rbe("main")
        self.sandbox.commit_all("main")
        self.config = str(self.sandbox.config)
        self.hook = self.sandbox.hook(BUILD_HOOK)

    def env(self, **extra):
        return self.sandbox.env(FAKE_HOOK=self.hook, **extra)

    def bdev(self, *args, env=None, cwd=None):
        return self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", *args,
                                 env=env or self.env(), cwd=cwd)

    def document(self, *args, **kwargs):
        result = self.bdev(*args, **kwargs)
        return result, json.loads(result.stdout)

    def node_calls(self):
        return [record for record in self.sandbox.records() if record["tool"] == "node"]

    def build_argv(self, index=-1):
        return self.node_calls()[index]["argv"][1:]

    def output_app(self, name="Debug_arm64"):
        return self.src / "out" / name / "Brave Browser Development.app"


class BuildTests(BuildTestCase):
    def test_bytecode_details_require_verbose_but_remain_in_logs(self):
        detail = "redirecting constructor from upstream/Class to brave/Class"
        self.hook = self.sandbox.hook(
            'if "build" in argv:\n'
            '    print("redirecting con", end="", flush=True)\n'
            '    print("structor from upstream/Class to brave/Class")\n'
            '    print("make field public in brave/Class")\n'
            '    print("[10/20] 1s F ACTION //chrome:empty_java__bytecode_rewrite(//toolchain)")\n'
            '    print("stdout:")\n'
            '    print("[11/20] 1s F ACTION //chrome:warning_java__bytecode_rewrite(//toolchain)")\n'
            '    print("stdout:")\n'
            '    print("WARNING: rewrite warning")\n'
            '    print("[12/20] build progress")\n'
            '    print("WARNING: bytecode diagnostic", file=sys.stderr)\n' + BUILD_HOOK)
        for verbosity in ("normal", "verbose", "quiet"):
            with self.subTest(verbosity=verbosity):
                result, document = self.document("build", "--verbosity", verbosity)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(detail in result.stderr, verbosity == "verbose")
                self.assertEqual("make field public in brave/Class" in result.stderr, verbosity == "verbose")
                self.assertEqual("[12/20] build progress" in result.stderr, verbosity != "quiet")
                self.assertEqual("WARNING: bytecode diagnostic" in result.stderr, verbosity != "quiet")
                self.assertEqual("empty_java__bytecode_rewrite" in result.stderr, verbosity == "verbose")
                self.assertEqual("warning_java__bytecode_rewrite" in result.stderr, verbosity != "quiet")
                self.assertEqual("WARNING: rewrite warning" in result.stderr, verbosity != "quiet")
                saved = Path(next(line.removeprefix("Log: ") for line in result.stderr.splitlines()
                                  if line.startswith("Log: "))).read_text()
                self.assertIn("empty_java__bytecode_rewrite", saved)
                self.assertIn("stdout:", saved)
                self.assertIn(detail, saved)
                self.assertIn("make field public in brave/Class", saved)

        direct = self.sandbox.bdev("--config", self.config, "--checkout", "main", "run", "build",
                                   tool="bpm", env=self.env())
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertIn(detail, direct.stdout)
        self.hook = self.sandbox.hook(Path(self.hook).read_text() + "\nraise SystemExit(1)\n")
        failed, _ = self.document("build", "--quiet")
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn(detail, failed.stderr)
        self.assertIn("WARNING: bytecode diagnostic", failed.stderr)

    def test_default_build_runs_the_package_build_in_core_and_verifies_the_app(self):
        result, document = self.document("build")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stderr)
        self.assertIn("Building Brave for macOS (Debug, arm64)", result.stderr)
        self.assertIn("Build mode: online (RBE/Siso requested)", result.stderr)
        self.assertIn("Output directory: " + str(self.src / "out" / "Debug_arm64"), result.stderr)
        for phase in ("Environment", "Readiness", "Source preparation", "Package build", "Output verification", "Source state"):
            self.assertRegex(result.stderr, phase + r" [0-9]+[.][0-9]{2}s")
        self.assertIn("$ pnpm run build", result.stderr)
        self.assertNotIn("/bin/node ", result.stderr)
        self.assertEqual(self.build_argv(), [
            "run", "build", "--target_os=mac", "--target_arch=arm64", "-C", "Debug_arm64", "Debug",
            "--use_remoteexec=true"])
        self.assertEqual(self.node_calls()[-1]["cwd"], str(self.core))
        artifact = document["artifacts"][0]
        self.assertEqual(artifact["path"], str(self.output_app()))
        self.assertTrue(artifact["verified"])
        self.assertEqual(document["data"]["build"]["artifact_status"], "verified")
        self.assertEqual(document["child_exit_code"], 0)
        launched = [r for r in self.sandbox.records() if r["tool"] not in ("node",)]
        self.assertEqual(launched, [], "build must never install or launch an app")

    def test_offline_and_configuration_change_the_generated_arguments(self):
        self.document("build", "--offline", "--configuration", "release")
        argv = self.build_argv()
        self.assertIn("--offline", argv)
        self.assertIn("Release", argv)
        self.assertIn("--channel=release", argv)
        self.assertNotIn("--use_remoteexec=true", argv)
        self.assertIn("Release_arm64", argv)

    def test_unknown_options_and_positionals_reach_the_package_command_unchanged(self):
        cases = {
            ("mac", "--upstream-option=value"): ["--upstream-option=value"],
            ("--upstream-option", "android"): ["--upstream-option", "android"],
            ("--json-lookalike", "--", "--json", "--checkout", "x", "--", "mac"):
                ["--json-lookalike", "--json", "--checkout", "x", "--", "mac"],
            ("--a", "", "--a", "b c", "-5", "café", "$(touch pwned)"):
                ["--a", "", "--a", "b c", "-5", "café", "$(touch pwned)"],
        }
        for args, tail in cases.items():
            with self.subTest(args=args):
                self.sandbox.record.unlink(missing_ok=True)
                result, document = self.document("build", *args)
                argv = self.build_argv()
                self.assertEqual(argv[-len(tail):], tail)
                self.assertIn("--target_os=mac", argv, "the default target is kept")
                self.assertEqual(document["command"], "build")
        self.assertFalse((self.sandbox.root / "pwned").exists())

    def test_missing_suite_option_values_and_conflicts_are_scaffold_errors(self):
        for args in (["--configuration"], ["--configuration", "purple"]):
            result, document = self.document("build", *args)
            self.assertEqual(document["error"]["code"], "INVALID_INPUT")
        self.assertEqual(self.node_calls(), [])

    def test_relative_build_directory_resolves_beneath_src_out_not_core(self):
        result, document = self.document("build", "-C", "Custom", env=self.env(FAKE_DECOYS="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.build_argv()
        self.assertEqual(argv.count("-C"), 1, "the generated output directory is left out")
        self.assertEqual(argv[argv.index("-C") + 1], "Custom")
        self.assertEqual(document["artifacts"][0]["output_dir"], str(self.src / "out" / "Custom"))
        self.assertEqual(document["data"]["build"]["effective"]["sources"]["output"], "forwarded")
        self.assertTrue((self.core / "Custom" / "Brave Browser Development.app").exists(), "decoy exists")

    def test_absolute_build_directory_and_architecture_override_are_used(self):
        custom = self.sandbox.root / "elsewhere" / "out"
        result, document = self.document("build", "-C%s" % custom)
        self.assertEqual(document["artifacts"][0]["output_dir"], str(custom))
        result, document = self.document("build", "--target_arch", "x64", "-C", "X64Out")
        argv = self.build_argv()
        self.assertNotIn("--target_arch=arm64", argv)
        self.assertEqual(document["data"]["build"]["effective"]["arch"], "x64")
        self.assertEqual(document["artifacts"][0]["arch"], "x64")

    def test_forwarded_options_that_contradict_explicit_selectors_fail_before_any_effect(self):
        for args in (["--configuration", "release", "--", "Debug"], ["android", "--target_os=mac"],
                     ["--offline", "--use_remoteexec=true"]):
            with self.subTest(args=args):
                result, document = self.document("build", *args)
                self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT")
                self.assertIn("example", document["error"]["details"])
        self.assertEqual(self.sandbox.records(), [])
        self.assertFalse((self.src / "out").exists())

    def test_zero_exit_with_unresolved_output_is_a_warning_for_build_and_sync_build(self):
        result, document = self.document("build", "--target=brave_unit_tests")
        self.assertEqual((result.returncode, document["status"], document["exit_code"]), (0, "ok", 0))
        self.assertEqual(document["child_exit_code"], 0)
        self.assertIsNone(document["error"])
        self.assertEqual(document["artifacts"], [])
        self.assertEqual(document["warnings"][0]["code"], "ARTIFACT_UNRESOLVED")
        build = document["data"]["build"]
        self.assertEqual((build["child_succeeded"], build["artifact_status"]), (True, "unresolved"))
        self.assertIn("brave_unit_tests", build["explanation"])
        result, document = self.document("sync-build", "--target=brave_unit_tests")
        self.assertEqual((result.returncode, document["status"], document["warnings"][0]["code"]),
                         (0, "ok", "ARTIFACT_UNRESOLVED"))

    def test_zero_exit_with_unresolved_output_fails_build_run_without_touching_the_browser(self):
        for name in ("build-run", "br", "sync-build-run", "sbr"):
            with self.subTest(command=name):
                self.sandbox.record.unlink(missing_ok=True)
                result, document = self.document(name, "--target=brave_unit_tests")
                self.assertEqual((result.returncode, document["status"], document["exit_code"]), (5, "error", 5))
                self.assertEqual(document["error"]["code"], "ARTIFACT_UNRESOLVED")
                self.assertEqual(document["child_exit_code"], 0)
                self.assertEqual(document["artifacts"], [])
                self.assertTrue(document["error"]["details"]["build"]["child_succeeded"])
                others = [r for r in self.sandbox.records() if r["tool"] != "node"]
                self.assertEqual(others, [], "no shutdown, install, or launch")

    def test_identified_but_missing_output_and_failed_children_are_errors(self):
        result, document = self.document("build", env=self.env(FAKE_NO_APP="1"))
        self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]), (5, "ARTIFACT_MISSING", 0))
        result, document = self.document("build", env=self.env(FAKE_EXIT="7"))
        self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]), (5, "CHILD_FAILED", 7))
        bundle = self.output_app()
        (bundle / "Contents" / "Info.plist").write_bytes(b"not a plist")
        result, document = self.document("build", env=self.env(FAKE_NO_APP="1"))
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISMATCH")

    def test_plan_runs_nothing_and_reports_the_effective_choices(self):
        result, document = self.document("build", "--plan", "-C", "Custom")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.node_calls(), [])
        plan = document["data"]["plan"]
        self.assertEqual(plan["effective"]["output_dir"], str(self.src / "out" / "Custom"))
        self.assertIn("build", [step["name"] for step in plan["steps"]])

    def test_combined_commands_send_the_forwarding_tail_only_to_build(self):
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("sync-build", "--tail-option", "value")
        calls = [r["argv"][1:] for r in self.node_calls()]
        self.assertEqual(calls[0], ["run", "sync"], "sync gets no user arguments")
        self.assertEqual(calls[1][-2:], ["--tail-option", "value"])
        self.assertEqual(sum("--tail-option" in call for call in calls), 1)

    def test_operations_are_recorded_outside_core_and_complete(self):
        result, document = self.document("build")
        record = json.loads((self.sandbox.config.parent / ".bdev" / "operations" /
                             (document["operation_id"] + ".json")).read_text())
        self.assertEqual((record["state"], record["status"]), ("complete", "ok"))
        self.assertEqual(record["checkout"], str(self.core))
        self.assertFalse(list(self.core.glob(".bdev*")))



class NinjaOutputTests(BuildTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.document("build")[0].returncode, 0)
        self.alternate = self.src / "out/Alternate"
        self.hook = self.sandbox.hook(BUILD_HOOK.replace(
            'out = build_dir if os.path.isabs(build_dir) else os.path.join(src, "out", build_dir)',
            'out = os.environ["FAKE_NINJA_OUTPUT"]'))

    def test_ninja_directory_options_keep_the_default_receipt_unchanged(self):
        (receipt,) = (self.sandbox.config.parent / ".bdev/outputs").glob("*/*.json")
        before = receipt.read_bytes()
        binary = self.output_app() / "Contents/MacOS/Brave Browser Development"
        stamp = binary.stat().st_mtime_ns
        (self.core / "changed.cc").write_text("new source\n")
        self.sandbox.commit_all("main")
        for tokens in (["--ninja=C:" + str(self.alternate)], ["--ninja", "C:" + str(self.alternate)]):
            with self.subTest(tokens=tokens):
                result, document = self.document("build", "--offline", *tokens,
                                                  env=self.env(FAKE_NINJA_OUTPUT=str(self.alternate)))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.alternate / self.output_app().name).exists())
                self.assertEqual(document["data"]["build"]["artifact_status"], "unresolved")
                self.assertIsNone(document["data"]["build"]["effective"]["output_dir"])
                self.assertEqual(binary.stat().st_mtime_ns, stamp)
                self.assertEqual(receipt.read_bytes(), before)
                self.assertEqual(self.build_argv()[-len(tokens):], tokens)

    def test_a_ninja_build_file_marks_the_known_directory_for_revalidation(self):
        result, document = self.document("build", "--ninja=f:other.ninja",
                                          env=self.env(FAKE_NINJA_OUTPUT=str(self.alternate)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["data"]["build"]["artifact_status"], "unresolved")
        (receipt,) = (self.sandbox.config.parent / ".bdev/outputs").glob("*/*.json")
        self.assertTrue(json.loads(receipt.read_text())["needs_revalidation"])

    def test_a_combined_plan_reports_unresolved_output_without_a_default_launch(self):
        result, document = self.document("build-run", "--plan", "--ninja=C:" + str(self.alternate))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(document["data"]["plan"]["effective"]["output_dir"])
        steps = document["data"]["plan"]["steps"]
        self.assertEqual(steps[-1]["status"], "unresolved")
        self.assertNotIn(str(self.output_app()), json.dumps(steps))

    def test_combined_build_stops_before_restart_for_directory_and_build_file_options(self):
        for tokens in (["--ninja=C:" + str(self.alternate)], ["--ninja", "f:other.ninja"]):
            with self.subTest(tokens=tokens):
                self.sandbox.record.unlink(missing_ok=True)
                result, document = self.document("build-run", *tokens,
                                                  env=self.env(FAKE_NINJA_OUTPUT=str(self.alternate)))
                self.assertEqual(result.returncode, 5, result.stderr)
                self.assertEqual(document["error"]["code"], "ARTIFACT_UNRESOLVED")
                self.assertFalse(any(r["tool"] in ("open", "osascript") for r in self.sandbox.records()))


class RemoteBuildReadinessTests(BuildTestCase):
    """Local remote-build configuration is required exactly when the effective compile mode is remote."""

    def break_rbe(self):
        self.sandbox.configure_rbe("main", siso_cache_dir=str(self.sandbox.root / "no-such-cache"))

    def test_a_remote_build_needs_its_local_configuration(self):
        self.break_rbe()
        for args in (["build"], ["build", "--use_remoteexec=true"], ["test", "brave_unit_tests"]):
            with self.subTest(args=args):
                result, document = self.document(*args)
                self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
                failing = {check["name"] for check in document["error"]["details"]["checks"]}
                self.assertIn("rbe-siso-cache", failing)
                self.assertEqual(self.node_calls(), [])

    def test_local_compilation_does_not_need_it(self):
        self.break_rbe()
        for args in (["build", "--offline"], ["build", "--use_remoteexec=false"], ["build", "--use_remoteexec", "false"]):
            with self.subTest(args=args):
                result, document = self.document(*args)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_default_fixture_configuration_is_enough_for_a_remote_build(self):
        result, document = self.document("build")
        self.assertEqual(result.returncode, 0, result.stderr)


class NonCompilingModeTests(BuildTestCase):
    """Core can exit zero without compiling; an app already at the expected path is not a new build."""

    MODES = (["--prepare_only"], ["--xcode_gen", "ios"], ["--xcode_gen=ios"], ["--ninja=n:"], ["--ninja", "n:"],
             ["--ninja=n"], ["--offline", "--ninja", "n:"])

    def output_states(self):
        directory = self.sandbox.config.parent / ".bdev" / "outputs"
        return [json.loads(path.read_text()) for path in sorted(directory.glob("*/*.json"))]

    def build_once(self):
        result, document = self.document("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        return document["operation_id"]

    def test_build_reports_the_child_success_but_no_verified_artifact(self):
        first = self.build_once()
        for mode in self.MODES:
            with self.subTest(mode=mode):
                result, document = self.document("build", *mode, env=self.env(FAKE_NO_APP="1"))
                self.assertEqual((result.returncode, document["status"], document["child_exit_code"]), (0, "ok", 0))
                self.assertEqual(document["artifacts"], [])
                self.assertEqual(document["warnings"][0]["code"], "ARTIFACT_UNRESOLVED")
                build = document["data"]["build"]
                self.assertEqual((build["child_succeeded"], build["artifact_status"]), (True, "unresolved"))
                option = next(token for token in mode if token.startswith(("--prepare", "--xcode", "--ninja")))
                self.assertIn(option.split("=")[0], build["explanation"])
                self.assertEqual(self.build_argv()[-len(mode):], mode, "the option still reaches the package command")
                (state,) = self.output_states()
                self.assertEqual(state["success"]["operation_id"], first, "the earlier build record is kept")

    def test_build_run_stops_before_touching_the_browser(self):
        self.build_once()
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.sandbox.record.unlink(missing_ok=True)
                result, document = self.document("build-run", *mode, env=self.env(FAKE_NO_APP="1"))
                self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]),
                                 (5, "ARTIFACT_UNRESOLVED", 0))
                self.assertEqual([r for r in self.sandbox.records() if r["tool"] != "node"], [])

    def test_a_ninja_dry_run_leaves_the_earlier_build_record_alone(self):
        first = self.build_once()
        (self.core / "browser").mkdir(exist_ok=True)
        (self.core / "browser" / "changed_since_the_build.cc").write_text("new work\n")
        self.sandbox.commit_all("main")
        binary = self.output_app() / "Contents" / "MacOS" / "Brave Browser Development"
        stamp = binary.stat().st_mtime_ns
        result, document = self.document("build", "--offline", "--ninja=n:", env=self.env(FAKE_NO_APP="1"))
        self.assertEqual(document["data"]["build"]["artifact_status"], "unresolved")
        self.assertEqual(binary.stat().st_mtime_ns, stamp)
        (state,) = self.output_states()
        self.assertEqual(state["success"]["operation_id"], first)

    def test_a_ninja_tool_run_may_change_the_output_so_the_earlier_record_needs_revalidation(self):
        self.build_once()
        self.document("build", "--ninja", "t:clean", env=self.env(FAKE_NO_APP="1"))
        self.assertTrue(self.output_states()[0]["needs_revalidation"])

    def test_a_gn_override_of_the_architecture_or_target_is_not_the_advertised_build(self):
        self.build_once()
        for args in (["--gn=target_cpu:x64"], ["--gn", 'target_cpu:"x64"'], ["--gn=target_os:android"]):
            with self.subTest(args=args):
                result, document = self.document("build", *args, env=self.env(FAKE_NO_APP="1"))
                self.assertEqual(document["data"]["build"]["artifact_status"], "unresolved")
                self.assertEqual(document["artifacts"], [])
        result, document = self.document("build", "--gn=target_cpu:arm64")
        self.assertEqual(document["data"]["build"]["artifact_status"], "verified", "a matching value is fine")
        result, document = self.document("build", "android", "--gn=target_os:mac")
        self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT")

    def test_preparing_without_compiling_does_not_clear_or_add_uncertainty_about_the_output(self):
        self.build_once()
        self.document("build", "--prepare_only", env=self.env(FAKE_NO_APP="1"))
        self.assertFalse(self.output_states()[0]["needs_revalidation"], "no compile step touches the output")
        self.document("build", env=self.env(FAKE_EXIT="1"))
        self.assertTrue(self.output_states()[0]["needs_revalidation"])
        self.document("build", "--prepare_only", env=self.env(FAKE_NO_APP="1"))
        self.assertTrue(self.output_states()[0]["needs_revalidation"], "an earlier failed rebuild is not forgiven")


if __name__ == "__main__":
    unittest.main()
