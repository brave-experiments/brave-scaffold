# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch preparation, sync, test commands, and drift through the real CLI."""

import hashlib
import json
import subprocess
import unittest

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
        self.assertEqual(document["error"]["repairs"][0]["argv"][:3], ["bdev", "drift", "--diff"])

    def test_drift_without_any_earlier_record_is_uncertain_ownership(self):
        self.update_patch_upstream()
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

    def test_sync_stops_when_local_work_could_be_overwritten(self):
        (self.core / "uncommitted.txt").write_text("work in progress\n")
        result, document = self.document("sync")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertEqual(self.node_calls(), [])
        self.assertEqual((self.core / "uncommitted.txt").read_text(), "work in progress\n")

    def test_sync_build_routes_each_tail_to_its_phase(self):
        self.document("sync-build", "-C", "Custom")
        calls = [r["argv"][1:] for r in self.node_calls()]
        self.assertEqual(calls[0], ["run", "sync"])
        self.assertIn("Custom", calls[1])

    def test_sync_plan_writes_nothing(self):
        result, document = self.document("sync", "--plan", "--force")
        self.assertEqual(self.node_calls(), [])
        self.assertEqual(document["data"]["plan"]["argv_arguments"], ["run", "sync", "--force"])


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class TestCommandTests(BuildTestCase):
    def test_suite_is_consumed_and_the_rest_is_forwarded_in_order(self):
        self.document("test", "brave_browser_tests", "--upstream-option", "value", "extra")
        argv = self.build_argv()
        self.assertEqual(argv[:3], ["run", "test", "brave_browser_tests"])
        self.assertEqual(argv[-3:], ["--upstream-option", "value", "extra"])
        self.assertIn("Debug", argv)

    def test_explicit_platform_filter_and_extra_positional(self):
        self.document("test", "mac", "brave_unit_tests", "extra", "--filter", "Example.*", "--x")
        argv = self.build_argv()
        self.assertEqual(argv[2], "brave_unit_tests")
        self.assertIn("--filter=Example.*", argv)
        self.assertEqual(argv[-2:], ["extra", "--x"])
        self.assertEqual(self.node_calls()[-1]["cwd"], str(self.core))

    def test_missing_or_malformed_suite_teaches_the_syntax(self):
        result, document = self.document("test", "--filter", "Example.*")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
        self.assertIn("bdev test brave_browser_tests", document["error"]["details"]["example"])
        result, document = self.document("test", "not-a-suite")
        self.assertEqual(document["error"]["code"], "INVALID_INPUT")
        self.assertIn("brave_browser_tests", document["error"]["details"]["common_suites"])
        self.assertEqual(self.node_calls(), [])

    def test_android_tests_are_unsupported_before_anything_is_loaded(self):
        write_executable(self.sandbox.bin / "direnv", "#!/bin/sh\necho used >> '%s'\nexit 1\n" %
                         (self.sandbox.root / "direnv-used"))
        result, document = self.document("test", "android", "brave_java_unit_tests")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "UNSUPPORTED_CAPABILITY"))
        self.sandbox.config.write_text(self.sandbox.config.read_text() + '\n[defaults]\nplatform = "android"\n')
        result, document = self.document("test", "brave_browser_tests")
        self.assertEqual(document["error"]["code"], "UNSUPPORTED_CAPABILITY")
        self.assertFalse((self.sandbox.root / "direnv-used").exists())
        self.assertEqual(self.node_calls(), [])

    def test_failed_suite_reports_the_child_exit(self):
        result, document = self.document("test", "brave_unit_tests", env=self.env(FAKE_EXIT="2"))
        self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]),
                         (5, "CHILD_FAILED", 2))


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
