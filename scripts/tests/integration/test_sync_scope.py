# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""A source sync stops before it can reset local work anywhere gclient manages."""

import hashlib
import subprocess
import unittest

from tests.integration.test_build import SKIP, BuildTestCase


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class SyncScopeTests(BuildTestCase):
    def setUp(self):
        super().setUp()
        self.workspace = self.src.parent
        (self.src / "chrome").mkdir()
        (self.src / "chrome" / "unpatched.cc").write_text("upstream\n")
        self.sandbox.commit_all("main")

    def add_dependency(self, name="v8", listed=True):
        return self.sandbox.add_dependency("main", name, listed)

    def assert_sync_stops(self, *paths):
        result, document = self.document("sync", "--force")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"), result.stderr)
        self.assertEqual(self.node_calls(), [], "the sync must not start")
        found = {item["path"] for item in document["error"]["details"].get("files", [])}
        for path in paths:
            self.assertIn(path, found)
        return document

    def test_an_edit_to_a_tracked_chromium_file_outside_the_patches_stops_the_sync(self):
        (self.src / "chrome" / "unpatched.cc").write_text("my experiment\n")
        self.assert_sync_stops("chrome/unpatched.cc")
        self.assertEqual((self.src / "chrome" / "unpatched.cc").read_text(), "my experiment\n")

    def test_a_staged_chromium_edit_stops_the_sync(self):
        (self.src / "chrome" / "unpatched.cc").write_text("staged\n")
        git(self.src, "add", "chrome/unpatched.cc")
        self.assert_sync_stops("chrome/unpatched.cc")

    def test_an_edit_in_a_dependency_repository_stops_the_sync(self):
        repo = self.add_dependency()
        (repo / "test.cc").write_text("my experiment\n")
        self.assert_sync_stops("v8/test.cc")
        self.assertEqual((repo / "test.cc").read_text(), "my experiment\n")

    def test_a_clean_dependency_repository_does_not_stop_the_sync(self):
        self.add_dependency()
        result, document = self.document("sync", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.node_calls()), 1)

    def test_an_untracked_file_blocks_when_incoming_paths_are_unknown(self):
        repo = self.add_dependency()
        (repo / "notes.txt").write_text("scratch\n")
        self.assert_sync_stops("v8/notes.txt")
        self.assertEqual((repo / "notes.txt").read_text(), "scratch\n")

    def test_incoming_tracked_file_cannot_replace_untracked_work(self):
        self.assert_incoming_collision_preserved(directory=False)

    def test_incoming_tracked_file_cannot_replace_an_untracked_directory(self):
        self.assert_incoming_collision_preserved(directory=True)

    def test_dependency_deletion_options_block_when_the_write_set_is_unknown(self):
        for flag in ("-D", "--delete_unused_deps", "--delete_unversioned_trees"):
            with self.subTest(flag=flag):
                result, document = self.document("sync", flag)
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
                self.assertEqual(self.node_calls(), [])

    def assert_incoming_collision_preserved(self, directory):
        repo = self.add_dependency()
        old = git(repo, "rev-parse", "HEAD").stdout.strip()
        incoming = repo / "incoming.cc"
        incoming.write_text("upstream\n")
        git(repo, "add", "incoming.cc")
        git(repo, "commit", "-q", "-m", "incoming file")
        new = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "reset", "--hard", old)
        if directory:
            incoming.mkdir()
            wanted = incoming / "notes.txt"
        else:
            wanted = incoming
        wanted.write_text("local work\n")
        self.hook = self.sandbox.hook('''
if "sync" in argv:
    import subprocess
    subprocess.run(["git", "-C", %r, "reset", "--hard", %r], check=True)
''' % (str(repo), new))
        self.assert_sync_stops("v8/" + str(wanted.relative_to(repo)))
        self.assertEqual(wanted.read_text(), "local work\n")
        self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), old)

    def test_a_repository_that_cannot_be_inspected_stops_the_sync(self):
        repo = self.add_dependency()
        (repo / ".git" / "HEAD").write_text("garbage\n")
        document = self.assert_sync_stops()
        self.assertIn("could not be inspected", document["error"]["message"])
        self.assertIn("v8", document["error"]["details"]["repository"])

    def test_an_unreadable_dependency_list_stops_the_sync(self):
        (self.workspace / ".gclient_entries").write_text("entries = not python\n")
        document = self.assert_sync_stops()
        self.assertIn(".gclient_entries", document["error"]["details"]["files"][0]["path"])

    def test_recorded_patch_results_are_not_local_work(self):
        target = self.src / "base" / "BUILD.gn"
        target.write_text("original\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "upstream content")
        target.write_text("patched\n")
        info = next((self.core / "patches").glob("*.patchinfo"))
        text = info.read_text().replace(
            hashlib.sha256(b"patched\n").hexdigest(), hashlib.sha256(target.read_bytes()).hexdigest())
        info.write_text(text)
        self.assertIn("base/BUILD.gn", git(self.src, "status", "--porcelain").stdout)
        result, document = self.document("sync", "--force")
        self.assertEqual(result.returncode, 0, result.stderr + str(document.get("error")))

    def test_an_edit_on_top_of_a_patch_result_stops_the_sync(self):
        (self.src / "base" / "BUILD.gn").write_text("patched and then edited by me\n")
        self.assert_sync_stops("base/BUILD.gn")

    def test_staged_work_is_protected_when_the_worktree_matches_a_patch(self):
        target = self.src / "base" / "BUILD.gn"
        target.write_text("staged work that must survive\n")
        git(self.src, "add", "base/BUILD.gn")
        target.write_text("patched\n")
        self.assert_sync_stops("base/BUILD.gn")
        self.assertEqual(target.read_text(), "patched\n")
        self.assertEqual(git(self.src, "show", ":base/BUILD.gn").stdout, "staged work that must survive\n")

    def test_staged_work_is_protected_when_the_worktree_matches_a_sync_baseline(self):
        target = self.src / "chrome" / "unpatched.cc"
        self.hook = self.sandbox.hook('''
if "sync" in argv:
    open(os.path.join(os.path.dirname(os.environ["BRAVE_CORE_DIR"]), "chrome", "unpatched.cc"), "w").write("generated\\n")
''')
        self.assertEqual(self.document("sync")[0].returncode, 0)
        self.sandbox.record.unlink()
        target.write_text("staged work that must survive\n")
        git(self.src, "add", "chrome/unpatched.cc")
        target.write_text("generated\n")
        self.assert_sync_stops("chrome/unpatched.cc")
        self.assertEqual(target.read_text(), "generated\n")
        self.assertEqual(git(self.src, "show", ":chrome/unpatched.cc").stdout, "staged work that must survive\n")

    def test_blanket_adoption_cannot_persist_trust_before_a_rejected_or_failed_sync(self):
        from scaffold.brave import records
        state = self.sandbox.config.parent / ".bdev" / "state" / records.checkout_key(self.core)
        baseline = state / "sync-baseline.json"
        wanted = self.src / "chrome" / "unpatched.cc"
        wanted.write_text("my work\n")
        for command, extra in (("sync", ()), ("sync", ("--plan",)), ("sync-build", ()),
                               ("sync-build-run", ("--plan",))):
            with self.subTest(command=command, extra=extra):
                result, document = self.document(command, "--adopt-local-changes", *extra,
                                                  env=self.env(FAKE_EXIT="9"))
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
                self.assertFalse(baseline.exists(), "rejection cannot make an ordinary retry trust these bytes")
                self.assertEqual(self.node_calls(), [])
                self.assertEqual(wanted.read_text(), "my work\n")
        self.assert_sync_stops("chrome/unpatched.cc")

    def test_rejected_adoption_keeps_an_existing_successful_sync_baseline(self):
        from scaffold.brave import records
        self.assertEqual(self.document("sync")[0].returncode, 0)
        baseline = (self.sandbox.config.parent / ".bdev" / "state" / records.checkout_key(self.core)
                    / "sync-baseline.json")
        before = baseline.read_bytes()
        self.sandbox.record.unlink()
        wanted = self.src / "chrome" / "unpatched.cc"
        wanted.write_text("my work\n")
        result, document = self.document("sync", "--adopt-local-changes", env=self.env(FAKE_EXIT="9"))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertEqual(baseline.read_bytes(), before)
        self.assertEqual(self.node_calls(), [])
        self.assert_sync_stops("chrome/unpatched.cc")
        self.assertEqual(wanted.read_text(), "my work\n")

    def test_adopting_does_not_hide_uncommitted_work_in_core(self):
        (self.core / "notes.txt").write_text("mine\n")
        result, document = self.document("sync", "--adopt-local-changes")
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertEqual(self.node_calls(), [])

    def test_changes_made_during_a_sync_are_recorded_and_later_edits_are_not(self):
        self.hook = self.sandbox.hook("""
if "sync" in argv:
    open(os.path.join(os.path.dirname(os.environ["BRAVE_CORE_DIR"]), "chrome", "unpatched.cc"), "w").write("generated\\n")
""")
        result, _ = self.document("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.sandbox.record.unlink()
        self.assertEqual(self.document("sync")[0].returncode, 0, "what the sync itself produced is not local work")
        (self.src / "chrome" / "unpatched.cc").write_text("mine\n")
        self.sandbox.record.unlink()
        self.assert_sync_stops("chrome/unpatched.cc")


if __name__ == "__main__":
    unittest.main()
