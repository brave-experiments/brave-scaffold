# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android support refresh must not overwrite local work, whatever triggers it."""

import json
import subprocess
import unittest

from tests.android_fixtures import GIT, make_support_repo
from tests.integration.test_android import AndroidTestCase
from tests.integration.test_build import SKIP

from scaffold.brave import patchformat  # noqa: E402  (tests.support puts the sources on sys.path)

BUILD_CONFIG = 'import("//build/config/x.gni")\nassert(host_os == "linux")\nkeep = true\n'
TARGET_A = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\n"


def git(repo, *args):
    subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True)


class ParsePatchTargetsTests(unittest.TestCase):
    def test_any_path_prefix_and_rename_forms_are_recognised(self):
        fork = ("diff --git forkSrcPrefix/a/one.gni forkDstPrefix/a/one.gni\n--- forkSrcPrefix/a/one.gni\n"
                "+++ forkDstPrefix/a/one.gni\n@@ -1 +1 @@\n-x\n+y\n")
        git_style = "diff --git a/b/two.cc b/b/two.cc\n--- a/b/two.cc\n+++ b/b/two.cc\n"
        created = "diff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n"
        renamed = "diff --git a/old.cc b/moved.cc\nsimilarity index 100%\nrename from old.cc\nrename to moved.cc\n"
        self.assertEqual(patchformat.parse_patch_targets(fork), {"a/one.gni"})
        self.assertEqual(patchformat.parse_patch_targets(git_style), {"b/two.cc"})
        self.assertEqual(patchformat.parse_patch_targets(created), {"new.txt"})
        self.assertEqual(patchformat.parse_patch_targets(renamed), {"old.cc", "moved.cc"})

    def test_text_without_recognisable_headers_is_not_guessed_at(self):
        for text in ("", "just some words\n", "--- \n+++ \n"):
            with self.subTest(text=text), self.assertRaises(patchformat.UnknownPatchFormat):
                patchformat.parse_patch_targets(text)


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class SupportRefreshLocalWorkTests(AndroidTestCase):
    def setUp(self):
        super().setUp()
        src = self.src
        (src / "build" / "config").mkdir(parents=True, exist_ok=True)
        (src / "build" / "config" / "support_fork.gni").write_text("original fork\n")
        (src / "build" / "config" / "BUILDCONFIG.gn").write_text(BUILD_CONFIG)
        (src / "support").mkdir()
        (src / "support" / "target_a.cc").write_text(TARGET_A)
        (src / "v8" / "gni").mkdir(parents=True)
        (src / "v8" / "gni" / "snapshot.gni").write_text("v8 original\n")
        subprocess.run(["git", "init", "-q", str(src / "v8")], check=True)
        git(src / "v8", "add", "-A")
        git(src / "v8", "commit", "-q", "-m", "v8")
        with open(src / ".git" / "info" / "exclude", "a") as stream:
            stream.write("/v8/\n")
        self.sandbox.commit_all("main")
        self.support = make_support_repo(self.sandbox.root / "realistic", {"v154": 154, "v155": 155}, realistic=True)
        self.assertEqual(self.setup_support().returncode, 0)
        result, document = self.document("build", "android")
        self.assertEqual(document["status"], "ok", result.stderr)
        self.assertEqual((src / "support" / "target_a.cc").read_text().splitlines()[1], "patched line2")
        self.assertEqual((src / "v8" / "gni" / "snapshot.gni").read_text(), "v8 patched\n")
        self.assertNotIn("assert(host_os", (src / "build" / "config" / "BUILDCONFIG.gn").read_text())

    def append(self, path, text):
        with open(path, "a") as stream:
            stream.write(text)

    def change_support_patch(self, name="support-a-prefix.patch", replacement="second"):
        patch = self.wc() / "patches" / name
        patch.write_text(patch.read_text().replace("patched line2", replacement + " line2"))

    def assert_blocked_without_changes(self, paths, expected_reason=None):
        before = {path: path.read_text() for path in paths}
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"), result.stderr)
        self.assertEqual({path: path.read_text() for path in paths}, before)
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [])
        if expected_reason:
            self.assertIn(expected_reason, str(document["error"]))
        return document

    def test_removing_a_copied_resource_does_not_reset_edited_source(self):
        target = self.src / "support" / "target_a.cc"
        self.append(target, "my experiment\n")
        (self.src / "third_party" / "jdk" / "current" / "release").unlink()
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.read_text().endswith("my experiment\n"))
        self.assertTrue((self.src / "third_party" / "jdk" / "current" / "release").exists(), "the resource returned")

    def test_edited_source_patched_with_a_fork_prefixed_header_blocks_the_patch_refresh(self):
        target = self.src / "build" / "config" / "support_fork.gni"
        self.append(target, "my experiment\n")
        self.change_support_patch()
        self.assert_blocked_without_changes([target, self.src / "support" / "target_a.cc"])

    def test_edited_source_in_a_nested_repository_blocks_the_patch_refresh(self):
        target = self.src / "v8" / "gni" / "snapshot.gni"
        self.append(target, "my experiment\n")
        self.change_support_patch()
        self.assert_blocked_without_changes([target])

    def test_a_file_the_support_script_edits_directly_is_protected(self):
        target = self.src / "build" / "config" / "BUILDCONFIG.gn"
        self.append(target, "my experiment\n")
        self.change_support_patch()
        document = self.assert_blocked_without_changes([target])
        self.assertIn("build/config/BUILDCONFIG.gn", str(document["error"]["details"]))

    def test_staged_edits_are_protected_too(self):
        target = self.src / "build" / "config" / "support_fork.gni"
        self.append(target, "staged experiment\n")
        git(self.src, "add", "build/config/support_fork.gni")
        self.change_support_patch()
        self.assert_blocked_without_changes([target])

    def test_an_edited_copied_resource_is_not_overwritten(self):
        release = self.src / "third_party" / "jdk" / "current" / "release"
        release.write_text("JAVA_VERSION=25 hand edited\n")
        self.assert_blocked_without_changes([release])

    def test_an_untouched_resource_is_replaced_when_the_support_resource_changes(self):
        (self.wc() / "res" / "jdk" / "current" / "release").write_text("JAVA_VERSION=26\n")
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.src / "third_party" / "jdk" / "current" / "release").read_text(), "JAVA_VERSION=26\n")

    def test_an_unrecognised_patch_format_stops_the_refresh_before_any_write(self):
        target = self.src / "support" / "target_a.cc"
        (self.wc() / "patches" / "support-a-prefix.patch").write_text("this is not a patch\n")
        self.assert_blocked_without_changes([target], "support-a-prefix.patch")

    def test_edits_are_only_protected_while_they_differ_from_what_support_wrote(self):
        self.change_support_patch(replacement="second")
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.src / "support" / "target_a.cc").read_text().splitlines()[1], "second line2")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class ResourceOwnershipTests(AndroidTestCase):
    """A support resource replaces an existing file only when that file is known not to hold local work."""

    RELEASE = "third_party/jdk/current/release"

    def setUp(self):
        super().setUp()
        self.assertEqual(self.setup_support().returncode, 0)
        self.release = self.src / "third_party" / "jdk" / "current" / "release"
        self.release.parent.mkdir(parents=True)

    def commit(self):
        self.sandbox.commit_all("main")

    def build(self):
        self.sandbox.record.unlink(missing_ok=True)
        return self.document("build", "android")

    def assert_blocked(self, path, content):
        result, document = self.build()
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"), result.stderr)
        self.assertIn(self.RELEASE, [item["path"] for item in document["error"]["details"]["files"]])
        self.assertEqual(path.read_text(), content)
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [], "nothing built")

    def state_file(self):
        (path,) = self.sandbox.config.parent.rglob("android-support.json")
        return path

    def test_a_tracked_unmodified_dependency_file_is_replaced_on_first_adoption(self):
        self.release.write_text("JAVA_VERSION=24 chromium supplied\n")
        self.commit()
        result, _ = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.release.read_text(), "JAVA_VERSION=25 v155\n")

    def test_a_tracked_dependency_file_with_local_edits_is_kept_on_first_adoption(self):
        self.release.write_text("JAVA_VERSION=24 chromium supplied\n")
        self.commit()
        self.release.write_text("local dependency experiment\n")
        self.assert_blocked(self.release, "local dependency experiment\n")

    def test_an_existing_untracked_file_of_unknown_origin_is_kept(self):
        self.release.write_text("something someone put here\n")
        self.assert_blocked(self.release, "something someone put here\n")

    def test_an_existing_copy_identical_to_the_support_resource_is_adopted(self):
        self.release.write_text("JAVA_VERSION=25 v155\n")
        result, _ = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_absent_destination_is_simply_created(self):
        result, _ = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.release.read_text(), "JAVA_VERSION=25 v155\n")

    def test_edits_to_a_copied_resource_survive_a_receipt_without_resource_history(self):
        self.assertEqual(self.build()[0].returncode, 0)
        self.release.write_text("JAVA_VERSION=25 hand edited\n")
        receipt = json.loads(self.state_file().read_text())
        self.assertTrue(receipt["targets"] is not None)
        del receipt["resources"]
        self.state_file().write_text(json.dumps(receipt))
        self.assert_blocked(self.release, "JAVA_VERSION=25 hand edited\n")

    def test_edits_to_a_copied_resource_survive_a_missing_receipt(self):
        self.assertEqual(self.build()[0].returncode, 0)
        self.release.write_text("JAVA_VERSION=25 hand edited\n")
        self.state_file().unlink()
        self.assert_blocked(self.release, "JAVA_VERSION=25 hand edited\n")

    def test_a_destination_first_declared_by_a_new_support_revision_is_checked_too(self):
        self.assertEqual(self.build()[0].returncode, 0)
        wc = self.wc()
        script = wc / "copyMacRes.sh"
        script.write_text(script.read_text() + 'patch_dependency "Extra" "third_party/extra" "" "res/extra/current" ""\n')
        (wc / "res" / "extra" / "current").mkdir(parents=True)
        (wc / "res" / "extra" / "current" / "info").write_text("from support\n")
        extra = self.src / "third_party" / "extra" / "current" / "info"
        extra.parent.mkdir(parents=True)
        extra.write_text("already here\n")
        result, document = self.build()
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertIn("third_party/extra/current/info", str(document["error"]["details"]["files"]))
        self.assertEqual(extra.read_text(), "already here\n")
        extra.unlink()
        self.assertEqual(self.build()[0].returncode, 0)
        self.assertEqual(extra.read_text(), "from support\n")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class SupportScriptEffectsTests(AndroidTestCase):
    def test_a_script_that_writes_outside_its_declared_scope_is_reported_and_not_recorded(self):
        (self.src / "chrome" / "other.cc").write_text("upstream\n")
        self.sandbox.commit_all("main")
        self.assertEqual(self.setup_support().returncode, 0)
        script = self.wc() / "applyPatches.sh"
        script.write_text(script.read_text() + 'echo "surprise" >> ../src/chrome/other.cc\n')
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"), result.stderr)
        self.assertEqual([item["path"] for item in document["error"]["details"]["files"]], ["chrome/other.cc"])
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [], "the build did not start")
        self.assertEqual(list(self.sandbox.config.parent.rglob("android-support.json")), [], "nothing was recorded")


if __name__ == "__main__":
    unittest.main()
