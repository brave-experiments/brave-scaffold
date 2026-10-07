# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch preparation, sync, test commands, and drift through the real CLI."""

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

from tests.integration.test_build import BUILD_HOOK, SKIP, BuildTestCase
from tests.support import write_executable

APPLY_HOOK = """
if "apply_patches" in argv:
    target = os.path.join(os.path.dirname(os.environ["BRAVE_CORE_DIR"]), "base", "BUILD.gn")
    open(target, "w").write("new patched content\\n")
else:
""" + "\n".join("    " + line for line in BUILD_HOOK.splitlines())


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class PatchPreparationTests(BuildTestCase):
    def apply_calls(self):
        return [r for r in self.node_calls() if "apply_patches" in r["argv"]]

    def update_patch_upstream(self, content="new patched content\n"):
        """Simulate pulling a Core commit that changes a patch and its metadata."""
        patch = self.core / "patches" / "base-BUILD.gn.patch"
        patch.write_text("diff --git a/base/BUILD.gn b/base/BUILD.gn\n-original\n+%s" % content)
        info = {"schemaVersion": 1, "patchChecksum": hashlib.sha256(patch.read_bytes()).hexdigest(),
                "appliesTo": [{"path": "base/BUILD.gn", "checksum": sha(content)}]}
        patch.with_suffix(".patchinfo").write_text(json.dumps(info))
        self.sandbox.commit_all("main")

    def test_current_patches_are_not_reapplied(self):
        self.document("build")
        self.document("build")
        self.assertEqual(self.apply_calls(), [])

    def test_changed_patch_with_untouched_source_is_applied_then_built(self):
        self.document("build")
        self.hook = self.sandbox.hook(APPLY_HOOK)
        self.update_patch_upstream()
        result, document = self.document("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.apply_calls()), 1)
        self.assertIn("--force_gn_gen", self.build_argv())
        self.assertTrue(document["data"]["build"]["patches_applied"])
        self.document("build")
        self.assertEqual(len(self.apply_calls()), 1, "the receipt now matches")

    def test_local_edit_to_a_patched_file_stops_before_anything_runs(self):
        self.document("build")
        calls = len(self.node_calls())
        self.hook = self.sandbox.hook(APPLY_HOOK)
        self.update_patch_upstream()
        (self.src / "base" / "BUILD.gn").write_text("my local experiment\n")
        result, document = self.document("build")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertEqual([f["path"] for f in document["error"]["details"]["files"]], ["base/BUILD.gn"])
        self.assertEqual(len(self.node_calls()), calls, "neither apply_patches nor build ran")
        self.assertEqual((self.src / "base" / "BUILD.gn").read_text(), "my local experiment\n")
        self.assertEqual(document["error"]["repairs"][0]["argv"][:3], ["bcore", "drift", "--diff"])

    def test_a_local_chmod_on_a_patched_file_stops_before_anything_runs(self):
        self.document("build")
        calls = len(self.node_calls())
        self.hook = self.sandbox.hook(APPLY_HOOK)
        self.update_patch_upstream()
        target = self.src / "base" / "BUILD.gn"
        target.chmod(0o755)
        result, document = self.document("build")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        (entry,) = document["error"]["details"]["files"]
        self.assertEqual(entry["path"], "base/BUILD.gn")
        self.assertIn("executable bit", entry["reason"])
        self.assertEqual(len(self.node_calls()), calls, "neither apply_patches nor build ran")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755, "the local change is untouched")
        target.chmod(0o644)
        result, document = self.document("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.apply_calls()), 1, "undoing the change lets patch preparation proceed")

    def test_a_receipt_written_before_modes_were_recorded_falls_back_to_the_mode_git_has(self):
        self.document("build")
        receipt = next((self.sandbox.config.parent / ".bcore" / "state").rglob("patch-receipt.json"))
        data = json.loads(receipt.read_text())
        self.assertIn("modes", data, "the receipt records each patched file's executable bit")
        del data["modes"]
        receipt.write_text(json.dumps(data))
        self.hook = self.sandbox.hook(APPLY_HOOK)
        self.update_patch_upstream()
        (self.src / "base" / "BUILD.gn").chmod(0o755)
        result, document = self.document("build")
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertIn("executable bit", document["error"]["details"]["files"][0]["reason"])

    def test_drift_without_any_earlier_record_is_uncertain_ownership(self):
        self.update_patch_upstream()
        (self.src / "base" / "BUILD.gn").write_text("my unrecorded edit\n")
        result, document = self.document("build")
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertIn("no earlier record", document["error"]["details"]["files"][0]["reason"])
        self.assertEqual(self.node_calls(), [])

    def test_unusable_patch_metadata_is_never_treated_as_clean(self):
        for info in (self.core / "patches").glob("*.patchinfo"):
            info.write_text("not json")
        self.sandbox.commit_all("main")
        result, document = self.document("build")
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertEqual(self.node_calls(), [])

    def test_new_patch_over_uncommitted_edits_is_a_conflict(self):
        self.document("build")
        (self.src / "chrome" / "browser").mkdir(parents=True)
        (self.src / "chrome" / "browser" / "x.cc").write_text("upstream\n")
        self.sandbox.commit_all("main")
        (self.src / "chrome" / "browser" / "x.cc").write_text("my edit\n")
        patch = self.core / "patches" / "chrome-browser-x.cc.patch"
        patch.write_text("diff --git a/chrome/browser/x.cc b/chrome/browser/x.cc\n-upstream\n+patched\n")
        result, document = self.document("build")
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertEqual(document["error"]["details"]["files"][0]["path"], "chrome/browser/x.cc")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class SyncTests(BuildTestCase):
    def test_sync_forwards_extras_to_sync_only(self):
        result, document = self.document("sync", "--force", "-C", "false")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [r["argv"][1:] for r in self.node_calls()]
        self.assertEqual(calls, [["run", "sync", "--force", "-C", "false"]])
        self.assertFalse((self.src / "out").exists(), "-C is a sync option here, not an output directory")
        revisions = document["data"]["sync"]
        self.assertIn("core_head", revisions["revisions_before"])

    def test_sync_leaves_local_work_protection_to_core(self):
        (self.core / "uncommitted.txt").write_text("work in progress\n")
        result, document = self.document("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(document["error"])
        calls = [r["argv"][1:] for r in self.node_calls()]
        self.assertEqual(calls, [["run", "sync"]], "sync is Core's command, without a guard added here")
        self.assertEqual((self.core / "uncommitted.txt").read_text(), "work in progress\n",
                         "the scaffold itself removes nothing")

    def test_sync_build_routes_each_tail_to_its_phase(self):
        self.document("sync-build", "-C", "Custom")
        calls = [r["argv"][1:] for r in self.node_calls()]
        self.assertEqual(calls[0], ["run", "sync"])
        self.assertIn("Custom", calls[1])

    def test_combined_commands_send_sync_args_to_the_sync_phase_only(self):
        for command in ("sync-build", "sync-build-run", "sb", "sbr"):
            with self.subTest(command=command):
                self.sandbox.record.unlink(missing_ok=True)
                result, document = self.document(command, "--sync-arg=--force", "--sync-arg", "-D", "--tail-option", "v")
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [r["argv"][1:] for r in self.node_calls()]
                self.assertEqual(calls[0], ["run", "sync", "--force", "-D"])
                self.assertEqual(sum("--force" in call or "-D" in call for call in calls), 1, "only the sync phase")
                self.assertEqual(calls[1][-2:], ["--tail-option", "v"])

    def test_combined_plans_show_the_sync_args_and_run_nothing(self):
        result, document = self.document("sync-build", "--plan", "--sync-arg=--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        (sync,) = [s for s in document["data"]["plan"]["steps"] if s["name"] == "sync"]
        self.assertIn("--force", sync["argv"])
        self.assertEqual(self.node_calls(), [])

    def test_sync_arg_belongs_to_the_combined_commands_only(self):
        for command in ("build", "build-run"):
            with self.subTest(command=command):
                result, document = self.document(command, "--sync-arg=--force")
                self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
                self.assertIn("sync-build", document["error"]["message"])
        self.assertEqual(self.node_calls(), [])

    def test_a_sync_style_dash_c_in_a_combined_command_is_refused_with_the_way_to_say_it(self):
        for tokens in (["-C", "false"], ["-C", "true"], ["-C", "0"], ["-C", "1"]):
            for plan in ([], ["--plan"]):
                with self.subTest(tokens=tokens, plan=plan):
                    result, document = self.document("sync-build", *tokens, *plan)
                    self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
                    self.assertIn("--sync-arg=-C --sync-arg=%s" % tokens[1], document["error"]["message"])
        self.assertEqual(self.node_calls(), [])
        self.assertFalse((self.src / "out").exists())

    def test_dash_c_keeps_its_meaning_outside_the_combined_commands_and_for_real_directories(self):
        result, document = self.document("sync-build", "-C", "Custom")
        self.assertEqual(result.returncode, 0, result.stderr)
        result, document = self.document("build", "-C", "false")
        self.assertEqual(result.returncode, 0, "build -C is always an output directory")
        self.assertTrue((self.src / "out" / "false").exists())
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("sync-build", "--sync-arg=-C", "--sync-arg=false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["argv"][1:] for r in self.node_calls()][0], ["run", "sync", "-C", "false"])

    def test_sync_plan_writes_nothing(self):
        result, document = self.document("sync", "--plan", "--force")
        self.assertEqual(self.node_calls(), [])
        self.assertEqual(document["data"]["plan"]["argv_arguments"], ["run", "sync", "--force"])


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class TestCommandTests(BuildTestCase):
    def test_profile_customization_filter_and_selected_output_reach_the_suite(self):
        pattern = ("BraveProfileCustomizationFeatureDisabledWebUITest.*:"
                   "BraveProfileCustomizationFeatureEnabledWebUITest.*:"
                   "ProfileCustomizationFileChooserDisabledBrowserTest.*:"
                   "ProfileCustomizationFileChooserEnabledBrowserTest.*:"
                   "SigninTest.ProfileCustomizationTest")
        self.hook = self.sandbox.hook('''
assert argv[1:3] == ["run", "test"]
assert argv[3] == "brave_browser_tests"
assert "Release" in argv and "--offline" in argv
assert argv[argv.index("-C") + 1] == "selected tests"
assert os.environ["BRAVE_CORE_DIR"] == os.getcwd()
print("suite build output", flush=True)
print("suite test output", file=sys.stderr, flush=True)
''')
        result, document = self.document("test", "brave_browser_tests", "--filter=" + pattern,
                                         "--configuration", "release", "--offline", "-C", "selected tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv().count("--filter=" + pattern), 1)
        self.assertNotIn("--use_remoteexec=true", self.build_argv())
        self.assertEqual(document["data"]["cwd"], str(self.core))
        self.assertIn("suite build output", result.stderr)
        self.assertIn("suite test output", result.stderr)
        records = self.sandbox.config.parent / ".bcore" / "operations"
        record = next(json.loads(path.read_text()) for path in records.glob("*.json")
                      if json.loads(path.read_text())["operation_id"] == document["operation_id"])
        self.assertEqual(record["details"]["effective"]["output_dir"], str(self.src / "out" / "selected tests"))
        diagnostic = Path(record["logs"]["diagnostic"]).read_text()
        self.assertIn("--filter=" + pattern, diagnostic)
        self.assertIn("suite test output", diagnostic)

    def test_unresolved_ninja_directory_does_not_crash_or_change_default_output_record(self):
        self.assertEqual(self.document("build")[0].returncode, 0)
        state = next((self.sandbox.config.parent / ".bcore" / "outputs").rglob("*.json"))
        before = state.read_bytes()
        result, document = self.document("test", "brave_browser_tests", "--ninja", "C:other-output")
        self.assertEqual((result.returncode, document["child_exit_code"]), (0, 0), result.stderr)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(self.build_argv()[-2:], ["--ninja", "C:other-output"])

    def test_suite_is_consumed_and_the_rest_is_forwarded_in_order(self):
        self.document("test", "brave_browser_tests", "--upstream-option", "value", "extra")
        argv = self.build_argv()
        self.assertEqual(argv[:3], ["run", "test", "brave_browser_tests"])
        self.assertEqual(argv[-4:-1], ["--upstream-option", "value", "extra"])
        self.assertTrue(argv[-1].startswith("--test-launcher-summary-output="), "generated options follow forwarded ones")
        self.assertIn("Debug", argv)

    def test_explicit_platform_filter_and_extra_positional(self):
        self.document("test", "mac", "brave_unit_tests", "extra", "--filter", "Example.*", "--x")
        argv = self.build_argv()
        self.assertEqual(argv[2], "brave_unit_tests")
        self.assertIn("--filter=Example.*", argv)
        self.assertEqual(argv[-3:-1], ["extra", "--x"])
        self.assertTrue(argv[-1].startswith("--test-launcher-summary-output="))
        self.assertEqual(self.node_calls()[-1]["cwd"], str(self.core))

    def test_missing_or_malformed_suite_teaches_the_syntax(self):
        result, document = self.document("test", "--filter", "Example.*")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
        self.assertIn("--filter narrows a named suite", document["error"]["message"])
        self.assertIn("bcore test brave_unit_tests", document["error"]["details"]["example"])
        self.assertEqual(self.node_calls(), [])
        result, document = self.document("test", "not-a-suite")
        self.assertEqual(document["error"]["code"], "INVALID_INPUT")
        self.assertIn("brave_browser_tests", document["error"]["details"]["common_suites"])
        self.assertEqual(self.node_calls(), [])

    def test_other_android_suites_are_unsupported_before_anything_is_loaded(self):
        write_executable(self.sandbox.bin / "direnv", "#!/bin/sh\necho used >> '%s'\nexit 1\n" %
                         (self.sandbox.root / "direnv-used"))
        result, document = self.document("test", "android", "brave_browser_tests")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "UNSUPPORTED_CAPABILITY"))
        result, document = self.document("test", "android", "brave_java_unit_tests")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEPENDENCY_INCOMPATIBLE"),
                         "a supported suite stops at the missing support working copy")
        self.sandbox.config.write_text(self.sandbox.config.read_text() + '\n[defaults]\nplatform = "android"\n')
        result, document = self.document("test", "brave_browser_tests")
        self.assertEqual(document["error"]["code"], "UNSUPPORTED_CAPABILITY")
        self.assertFalse((self.sandbox.root / "direnv-used").exists())
        self.assertEqual(self.node_calls(), [])

    def test_a_forwarded_android_target_with_another_suite_is_unsupported_before_anything_is_loaded(self):
        write_executable(self.sandbox.bin / "direnv", "#!/bin/sh\necho used >> '%s'\nexit 1\n" %
                         (self.sandbox.root / "direnv-used"))
        for args in (["test", "brave_browser_tests", "--target_os=android"],
                     ["test", "brave_browser_tests", "--target_os", "android", "extra"],
                     ["test", "brave_browser_tests", "--filter", "A.*", "--", "--target_os=android"]):
            with self.subTest(args=args):
                result, document = self.document(*args)
                self.assertEqual((result.returncode, document["error"]["code"]), (2, "UNSUPPORTED_CAPABILITY"))
        result, document = self.document("test", "mac", "brave_browser_tests", "--target_os=android")
        self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT", "an explicit macOS request still conflicts")
        self.assertFalse((self.sandbox.root / "direnv-used").exists())
        self.assertEqual([r for r in self.sandbox.records() if r["tool"] != "node"], [])
        self.assertEqual(self.node_calls(), [])

    def test_an_explicit_forwarded_mac_target_overrides_an_android_default(self):
        self.sandbox.config.write_text(self.sandbox.config.read_text() + '\n[defaults]\nplatform = "android"\n')
        result, document = self.document("test", "brave_unit_tests", "--target_os=mac")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--target_os=mac", self.build_argv())

    def test_failed_suite_reports_the_child_exit(self):
        result, document = self.document("test", "brave_unit_tests", env=self.env(FAKE_EXIT="2"))
        self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]),
                         (5, "CHILD_FAILED", 2))


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class UnrecordedPatchTargetTests(BuildTestCase):
    """A patch without metadata is applied only when none of its targets holds local work."""

    def setUp(self):
        super().setUp()
        for name in ("keep.cc", "staged.cc", "gone.cc", "old.cc", "staged_gone.cc"):
            (self.src / "base" / name).write_text(name + "\n")
        self.sandbox.commit_all("main")
        self.assertEqual(self.document("build")[0].returncode, 0)

    def add_patch(self, *targets, name="extra.patch"):
        text = "".join("diff --git a/base/%s b/base/%s\n--- a/base/%s\n+++ b/base/%s\n@@ -1 +1 @@\n-x\n+y\n" % (
            (target,) * 4) for target in targets)
        (self.core / "patches" / name).write_text(text)
        self.sandbox.commit_all("main")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.src), *args], check=True, capture_output=True)

    def assert_stops(self, *targets, ok=False):
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("build")
        if ok:
            self.assertEqual(result.returncode, 0, document.get("error"))
            return
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        paths = {item["path"] for item in document["error"]["details"]["files"]}
        self.assertEqual(paths, {"base/" + name for name in targets})
        self.assertEqual(self.node_calls(), [], "nothing was applied or built")

    def test_untouched_targets_do_not_block(self):
        self.add_patch("keep.cc")
        self.assert_stops(ok=True)

    def test_each_kind_of_local_work_on_a_target_blocks_before_anything_runs(self):
        self.add_patch("staged.cc", "gone.cc", "staged_gone.cc", "moved_to.cc", "made_locally.cc", "keep.cc")
        (self.src / "base" / "staged.cc").write_text("staged edit\n")
        self.git("add", "base/staged.cc")
        (self.src / "base" / "gone.cc").unlink()
        self.git("rm", "-q", "base/staged_gone.cc")
        self.git("mv", "base/old.cc", "base/moved_to.cc")
        (self.src / "base" / "made_locally.cc").write_text("mine\n")
        self.assert_stops("staged.cc", "gone.cc", "staged_gone.cc", "moved_to.cc", "made_locally.cc")

    def test_the_old_name_of_a_renamed_file_blocks_too(self):
        self.add_patch("old.cc")
        self.git("mv", "base/old.cc", "base/moved_to.cc")
        self.assert_stops("old.cc")

    def test_a_patch_whose_targets_cannot_be_read_stops(self):
        (self.core / "patches" / "odd.patch").write_text("this is not a patch\n")
        self.sandbox.commit_all("main")
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("build")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertIn("odd.patch", json.dumps(document["error"]))
        self.assertEqual(self.node_calls(), [])


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class DriftTests(BuildTestCase):
    def test_clean_and_drifted_reports(self):
        result, document = self.document("drift")
        self.assertEqual(document["data"]["drifted"], [])
        self.assertTrue(document["data"]["metadata_complete"])
        (self.src / "base" / "BUILD.gn").write_text("changed\n")
        result, document = self.document("drift", "--diff")
        files = document["data"]["drifted"]
        self.assertEqual([f["path"] for f in files], ["base/BUILD.gn"])
        self.assertIn("source changed after patch applied", files[0]["reasons"])

    def test_missing_metadata_is_incomplete_evidence_not_a_clean_result(self):
        for info in (self.core / "patches").glob("*.patchinfo"):
            info.unlink()
        result, document = self.document("drift")
        self.assertFalse(document["data"]["metadata_complete"])
        self.assertEqual(document["warnings"][0]["code"], "INCOMPLETE_EVIDENCE")
        self.assertNotIn("match their metadata", json.dumps(document["data"]))

    def test_drift_is_read_only(self):
        from tests.support import tree_snapshot
        before = tree_snapshot(self.core.parents[3])
        self.document("drift", "--diff")
        self.assertEqual(before, tree_snapshot(self.core.parents[3]))


if __name__ == "__main__":
    unittest.main()
