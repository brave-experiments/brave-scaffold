# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android support refresh must not overwrite local work, whatever triggers it."""

import hashlib
import json
import subprocess
import unittest

from tests.android_fixtures import COPY_SCRIPT, GIT, make_support_repo
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
        entries = src.parent / ".gclient_entries"
        entries.write_text(entries.read_text().replace("}\n", "  'src/v8': 'https://example.invalid/v8.git',\n}\n"))
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

    def assert_blocked_without_changes(self, paths, expected_reason=None, skip=True):
        before = {path: path.read_text() for path in paths}
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("build", "android", *(["--skip-support-refresh"] if skip else []))
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

    def test_refresh_replaces_edited_patch_targets_in_both_repositories(self):
        paths = [self.src / "build/config/support_fork.gni", self.src / "v8/gni/snapshot.gni"]
        for target in paths:
            self.append(target, "my experiment\n")
        self.change_support_patch()
        result, _ = self.document("build", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        for target in paths:
            self.assertNotIn("my experiment", target.read_text())

    def test_skip_refresh_keeps_direct_source_edits(self):
        target = self.src / "build/config/BUILDCONFIG.gn"
        self.append(target, "my experiment\n")
        self.change_support_patch()
        self.assert_blocked_without_changes([target])

    def test_skip_refresh_keeps_staged_edits(self):
        target = self.src / "build" / "config" / "support_fork.gni"
        self.append(target, "staged experiment\n")
        git(self.src, "add", "build/config/support_fork.gni")
        self.change_support_patch()
        self.assert_blocked_without_changes([target])

    def test_skip_refresh_keeps_index_when_worktree_matches_support(self):
        target = self.src / "build" / "config" / "support_fork.gni"
        generated = target.read_text()
        target.write_text("staged work\n")
        git(self.src, "add", "build/config/support_fork.gni")
        target.write_text(generated)
        self.change_support_patch()
        self.assert_blocked_without_changes([target])
        indexed = subprocess.run(["git", "-C", str(self.src), "show", ":build/config/support_fork.gni"],
                                 check=True, capture_output=True, text=True).stdout
        self.assertEqual(indexed, "staged work\n")

    def test_named_patch_repositories_still_require_complete_discovery(self):
        target = self.src / "support" / "target_a.cc"
        (self.src.parent / ".gclient_entries").unlink()
        self.change_support_patch()
        self.assert_blocked_without_changes([target], "incomplete evidence", skip=False)

    def test_skip_refresh_keeps_an_edited_copied_resource(self):
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
        self.assert_blocked_without_changes([target], "support-a-prefix.patch", skip=False)

    def test_changed_support_patch_is_reapplied(self):
        self.change_support_patch(replacement="second")
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.src / "support" / "target_a.cc").read_text().splitlines()[1], "second line2")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class ResourceRefreshTests(AndroidTestCase):
    """Refresh replaces stale and unrecorded resources; skip mode leaves them alone."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.setup_support().returncode, 0)
        self.release = self.src / "third_party/jdk/current/release"
        self.release.parent.mkdir(parents=True)

    def build(self, *options):
        self.sandbox.record.unlink(missing_ok=True)
        return self.document("build", "android", *options)

    def state_file(self):
        (path,) = self.sandbox.config.parent.rglob("android-support.json")
        return path

    def test_unrecorded_local_resource_is_replaced(self):
        self.release.write_text("wanted local work\n")
        result, _ = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.release.read_text(), "JAVA_VERSION=25 v155\n")

    def test_unrecorded_binary_resource_is_replaced(self):
        origin = self.wc() / "res/jdk/current/tool"
        copied = self.release.parent / "tool"
        origin.write_bytes(b"\xcf\xfa\xed\xfe support tool")
        copied.write_bytes(b"\xcf\xfa\xed\xfe local tool")
        result, _ = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(copied.read_bytes(), origin.read_bytes())

    def test_missing_receipt_refreshes_edited_resource(self):
        self.assertEqual(self.build()[0].returncode, 0)
        self.state_file().unlink()
        self.release.write_text("JAVA_VERSION=25 local edit\n")
        result, _ = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.release.read_text(), "JAVA_VERSION=25 v155\n")

    def test_skip_blocks_unrecorded_refresh_but_accepts_current_support(self):
        self.release.write_text("local work\n")
        result, document = self.build("--skip-support-refresh")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertEqual(self.release.read_text(), "local work\n")
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [])
        self.assertEqual(self.build()[0].returncode, 0)
        result, _ = self.build("--skip-support-refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--skip-support-refresh", str(self.node_calls()))

    def test_doctor_uses_the_refresh_decision_without_writing(self):
        with open(self.core / ".env", "a") as stream:
            stream.write("brave_services_key=fixture-services-key\n")
        self.release.write_text("local work\n")
        result, document = self.document("doctor", "android")
        self.assertEqual(result.returncode, 0, result.stderr)
        checks = {check["name"]: check for check in document["checks"]}
        self.assertEqual(checks["android-support-currency"]["status"], "warning")
        message = checks["android-support-currency"]["summary"]
        self.assertIn("Refresh needed", message)
        self.assertIn("checks pass", message)
        self.assertIn("without a backup", message)
        self.assertNotIn("will refresh", message)
        self.assertEqual(self.release.read_text(), "local work\n")
        self.assertEqual(self.build()[0].returncode, 0)
        result, document = self.document("doctor", "android")
        checks = {check["name"]: check for check in document["checks"]}
        self.assertEqual(checks["android-support-currency"]["status"], "pass")
        (self.src.parent / ".gclient_entries").unlink()
        (self.wc() / "res/jdk/current/release").write_text("JAVA_VERSION=26\n")
        result, document = self.document("doctor", "android")
        self.assertEqual(result.returncode, 3, result.stderr)
        checks = {check["name"]: check for check in document["checks"]}
        self.assertEqual(checks["android-support-currency"]["status"], "blocker")

    def test_a_known_resigned_copy_can_be_refreshed(self):
        origin = self.wc() / "res/jdk/current/tool"
        origin.write_bytes(b"\xcf\xfa\xed\xfe support tool")
        script = (COPY_SCRIPT +
                  'patch_dependency "Tool" "third_party/tools" "" "res/jdk/current/tool" ""\n'
                  'printf signed >> "$src_root/third_party/tools/tool"\n')
        manifest = self.sandbox.scripts / "src/scaffold/brave/support_script_contracts.json"
        contracts = json.loads(manifest.read_text())
        contracts[hashlib.sha256(script.encode()).hexdigest()] = {
            "script": "copyMacRes.sh", "patches": [], "direct": [],
            "resources": [["third_party/jdk", "res/jdk/current"],
                          ["third_party/tools", "res/jdk/current/tool"]]}
        manifest.write_text(json.dumps(contracts))
        (self.wc() / "copyMacRes.sh").write_text(script)
        subprocess.run([*GIT, "-C", str(self.wc()), "add", "."], check=True, capture_output=True)
        subprocess.run([*GIT, "-C", str(self.wc()), "commit", "-qm", "add resource signing"], check=True, capture_output=True)
        self.assertEqual(self.build()[0].returncode, 0)
        self.assertIsNotNone(json.loads(self.state_file().read_text())["inputs"])
        copied = self.src / "third_party/tools/tool"
        self.assertEqual(copied.read_bytes(), origin.read_bytes() + b"signed")
        self.assertEqual(self.build()[0].returncode, 0)
        self.assertEqual(copied.read_bytes(), origin.read_bytes() + b"signed")
        origin.write_bytes(b"\xcf\xfa\xed\xfe replacement support tool")
        result, _ = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(copied.read_bytes(), origin.read_bytes() + b"signed")



@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class SupportScriptEffectsTests(AndroidTestCase):
    def test_unknown_direct_write_is_blocked_before_it_replaces_local_work(self):
        (self.src / "chrome" / "other.cc").write_text("upstream\n")
        self.sandbox.commit_all("main")
        (self.src / "chrome" / "other.cc").write_text("wanted local work\n")
        self.assertEqual(self.setup_support().returncode, 0)
        script = self.wc() / "applyPatches.sh"
        script.write_text(script.read_text() + 'echo "surprise" > ../src/chrome/other.cc\n')
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"), result.stderr)
        self.assertEqual((self.src / "chrome" / "other.cc").read_text(), "wanted local work\n")
        self.assertEqual([r for r in self.node_calls() if "build" in r["argv"]], [], "the build did not start")
        self.assertEqual(list(self.sandbox.config.parent.rglob("android-support.json")), [], "nothing was recorded")

    def test_unknown_indirect_write_is_blocked_before_the_helper_runs(self):
        self.assertEqual(self.setup_support().returncode, 0)
        target = self.src / "chrome" / "untracked.cc"
        target.write_text("wanted untracked work\n")
        (self.wc() / "helper.sh").write_text('echo replacement > ../src/chrome/untracked.cc\n')
        script = self.wc() / "applyPatches.sh"
        script.write_text(script.read_text() + 'bash ./helper.sh\n')
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertEqual(target.read_text(), "wanted untracked work\n")

    def test_unknown_verify_code_is_not_executed_by_doctor(self):
        self.assertEqual(self.setup_support().returncode, 0)
        target = self.src / "chrome" / "untracked.cc"
        target.write_text("wanted work\n")
        script = self.wc() / "copyMacRes.sh"
        script.write_text('echo replacement > ../src/chrome/untracked.cc\n' + script.read_text())
        self.document("doctor", "android")
        self.assertEqual(target.read_text(), "wanted work\n")

    def test_incomplete_repository_discovery_blocks_support_writes(self):
        self.assertEqual(self.setup_support().returncode, 0)
        (self.src.parent / ".gclient_entries").unlink()
        result, document = self.document("build", "android")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertFalse((self.src / "SUPPORT_PATCHED").exists())


if __name__ == "__main__":
    unittest.main()
