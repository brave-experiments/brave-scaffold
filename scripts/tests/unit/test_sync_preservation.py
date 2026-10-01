# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Sync guards protect threatened bytes while allowing work outside the write set."""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import tests.support  # noqa: F401
from scaffold.brave import sync_scope


class SyncFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="scaffold-sync-preservation-")
        self.addCleanup(self.directory.cleanup)
        self.src = Path(self.directory.name) / "src"
        self.core = self.src / "brave"
        self.core.mkdir(parents=True)
        self.identity = SimpleNamespace(src=self.src, core=self.core, workspace=self.src.parent)
        for repo in (self.src, self.core):
            self.git(repo, "init", "-q")
            (repo / "work.cc").write_text("upstream\n")
            self.git(repo, "add", "work.cc")
            self.git(repo, "commit", "-qm", "base")
        (self.src / ".git/info/exclude").write_text("/brave/\n")
        (self.identity.workspace / ".gclient_entries").write_text("entries = {}\n")
        (self.core / ".brave_gclient_entries").write_text("entries = {}\n")
        (self.core / ".git/info/exclude").write_text("/.brave_gclient_entries\n")
        self.scope = sync_scope.SyncScope([self.src, self.core])

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.com",
                               "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)



class SyncPreservationTests(SyncFixture):
    def guard(self, reset=(), writes=(), unknown=()):
        return sync_scope.local_work(self.identity, self.scope, set(), {},
                                     reset_repositories=set(reset), writes=set(writes), unknown_writes=list(unknown))

    def test_core_edits_and_staged_content_survive_an_unmanaged_solution(self):
        target = self.core / "work.cc"
        target.write_text("staged\n")
        self.git(self.core, "add", "work.cc")
        target.write_text("working\n")
        note = self.core / "notes.txt"
        note.write_text("scratch\n")
        self.assertEqual(self.guard(reset=[self.src]), [])
        self.assertEqual(target.read_text(), "working\n")
        self.assertEqual(self.git(self.core, "show", ":work.cc").stdout, "staged\n")
        self.assertEqual(note.read_text(), "scratch\n")

    def test_skipped_chromium_sync_preserves_unpatched_staged_and_untracked_work(self):
        target = self.src / "work.cc"
        target.write_text("staged\n")
        self.git(self.src, "add", "work.cc")
        target.write_text("working\n")
        (self.src / "notes.txt").write_text("scratch\n")
        self.assertEqual(self.guard(), [])
        self.assertEqual(self.git(self.src, "show", ":work.cc").stdout, "staged\n")
        self.assertEqual(target.read_text(), "working\n")

    def test_hook_output_is_checked_even_when_chromium_sync_is_skipped(self):
        target = self.core / "work.cc"
        target.write_text("my generated-file edit\n")
        conflicts = self.guard(writes=[target])
        self.assertEqual([item["path"] for item in conflicts], ["brave/work.cc"])
        self.assertIn("hook", conflicts[0]["reason"])
        self.assertEqual(target.read_text(), "my generated-file edit\n")

    def test_untracked_directory_that_obstructs_a_hook_output_stops(self):
        target = self.core / "output"
        target.mkdir()
        (target / "notes.txt").write_text("wanted\n")
        self.assertEqual([item["path"] for item in self.guard(writes=[target])], ["brave/output/notes.txt"])

    def test_preserved_work_does_not_become_generated_sync_evidence(self):
        target = self.src / "work.cc"
        target.write_text("developer work\n")
        before = sync_scope.snapshot(self.identity, self.scope)
        sync_scope.checkpoint(self.identity, Path(self.directory.name), before=before)
        baseline = sync_scope.read_baseline(self.identity, Path(self.directory.name))
        conflicts = sync_scope.local_work(self.identity, self.scope, set(), baseline)
        self.assertEqual([item["path"] for item in conflicts], ["work.cc"])

    def test_a_new_sync_output_is_recorded_but_an_unchanged_old_output_keeps_its_evidence(self):
        root = Path(self.directory.name)
        before = sync_scope.snapshot(self.identity, self.scope)
        target = self.src / "work.cc"
        target.write_text("generated\n")
        sync_scope.checkpoint(self.identity, root, before=before)
        baseline = sync_scope.read_baseline(self.identity, root)
        self.assertEqual(baseline["."]["work.cc"], hashlib.sha256(b"generated\n").hexdigest())
        sync_scope.checkpoint(self.identity, root, before=sync_scope.snapshot(self.identity, self.scope))
        self.assertEqual(sync_scope.read_baseline(self.identity, root), baseline)

    def test_unknown_hook_reports_the_threatened_file_and_operation(self):
        (self.core / "work.cc").write_text("wanted\n")
        conflicts = self.guard(unknown=["unreviewed hook custom-generator"])
        self.assertEqual(conflicts[0]["path"], "brave/work.cc")
        self.assertIn("custom-generator", conflicts[0]["reason"])

    def test_identified_untracked_patch_output_can_be_replaced_by_reset(self):
        target = self.src / 'generated.cc'
        target.write_text('identified patch output\n')
        conflicts = sync_scope.local_work(self.identity, self.scope, {target}, {},
                                          reset_repositories={self.src})
        self.assertEqual(conflicts, [])
        self.assertEqual(target.read_text(), 'identified patch output\n')
        self.assertEqual([item['path'] for item in sync_scope.local_work(
            self.identity, self.scope, set(), {}, reset_repositories={self.src})], ['generated.cc'])

    def test_a_developer_symlink_is_not_identified_as_generated_file_bytes(self):
        target = self.src / 'work.cc'
        elsewhere = Path(self.directory.name) / 'wanted.cc'
        elsewhere.write_text('upstream\n')
        target.unlink()
        target.symlink_to(elsewhere)
        conflicts = sync_scope.local_work(self.identity, self.scope, {target}, {},
                                          reset_repositories={self.src})
        self.assertEqual([item['path'] for item in conflicts], ['work.cc'])
        self.assertTrue(target.is_symlink())

    def test_reset_upstream_preserves_local_commits_by_stopping_before_dispatch(self):
        self.git(self.src, 'remote', 'add', 'origin', 'https://example.invalid/repo')
        self.git(self.src, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git(self.src, 'branch', '--set-upstream-to=origin/main')
        target = self.src / 'work.cc'
        target.write_text('committed local work\n')
        self.git(self.src, 'add', 'work.cc')
        self.git(self.src, 'commit', '-qm', 'wanted work')
        conflicts = self.guard(reset=[self.src])
        self.assertTrue(any('local commits' in item['reason'] for item in conflicts))
        self.assertEqual(target.read_text(), 'committed local work\n')
        self.assertEqual(self.guard(), [])


class ChromiumSyncDecisionTests(SyncFixture):
    def test_explicit_false_wins_over_force_and_configuration_change(self):
        from scaffold.brave.sync_model import chromium_will_sync
        self.assertFalse(chromium_will_sync(self.identity, {'chromium_option': False, 'force': True,
                                                          'gclient_changed': True}))

    def test_supported_sync_install_prefix_keeps_the_core_workflow(self):
        from scaffold.brave.sync_model import sync_entrypoint
        core_sync = "pnpm install --frozen-lockfile --yes && node ./build/commands/scripts/sync.ts"
        self.assertEqual(sync_entrypoint({'sync': core_sync,
            'pnpm:devPreinstall': 'node ./build/commands/scripts/devPreinstall.ts'}), 'pnpm')
        self.assertEqual(sync_entrypoint({'sync': 'node ./build/commands/scripts/sync.ts'}), 'node')
        self.assertIsNone(sync_entrypoint({'sync': core_sync, 'postsync': 'node custom.js'}))
        self.assertIsNone(sync_entrypoint({'sync': core_sync, 'postinstall': 'node custom.js'}))

    def test_dangling_ignored_version_sidecar_link_stops_before_write(self):
        from scaffold.brave import patches
        self.src.joinpath('chrome').mkdir()
        link = self.src / 'chrome/VERSION.chromium'
        destination = Path(self.directory.name) / 'wanted-version'
        link.symlink_to(destination)
        self.src.joinpath('.git/info/exclude').write_text('/brave/\n/chrome/VERSION.chromium\n')
        conflicts, _ = patches.write_set_conflicts(self.identity, [], {}, {}, None)
        self.assertIn('chrome/VERSION.chromium', {item['path'] for item in conflicts})
        self.assertTrue(link.is_symlink())
        self.assertFalse(destination.exists())

    def test_explicit_true_requires_sync(self):
        from scaffold.brave.sync_model import chromium_will_sync
        self.assertTrue(chromium_will_sync(self.identity, {'chromium_option': True}))

    def test_matching_receipt_and_ref_skip_sync_but_a_changed_gclient_does_not(self):
        import json
        from scaffold.brave.sync_model import chromium_will_sync
        gclient = self.identity.workspace / '.gclient'
        gclient.write_text('solutions = []\n')
        receipt = {'chromiumRef': 'HEAD', 'gclientTimestamp': str(gclient.stat().st_mtime_ns / 1_000_000)}
        path = self.identity.workspace / '.brave_latest_successful_sync.json'
        path.write_text(json.dumps(receipt))
        config = {'chromium_option': None, 'force': False, 'gclient_changed': False, 'chromium_ref': 'HEAD', 'gclient_timestamp': receipt['gclientTimestamp']}
        self.assertFalse(chromium_will_sync(self.identity, config))
        self.assertTrue(chromium_will_sync(self.identity, dict(config, gclient_changed=True)))
        self.assertTrue(chromium_will_sync(self.identity, dict(config, force=True)))
        receipt['gclientTimestamp'] = '0'
        path.write_text(json.dumps(receipt))
        self.assertTrue(chromium_will_sync(self.identity, config))

    def test_skipping_sync_does_not_skip_the_version_writer_guard(self):
        from scaffold.brave import sync, sync_model
        import json
        self.core.joinpath('patches').mkdir()
        target = self.src / 'work.cc'
        patchfile = self.core / 'patches/work.patch'
        patchfile.write_text('diff --git a/work.cc b/work.cc\n--- a/work.cc\n+++ b/work.cc\n-old\n+new\n')
        patchfile.with_suffix('.patchinfo').write_text(json.dumps({'schemaVersion': 1,
            'patchChecksum': hashlib.sha256(patchfile.read_bytes()).hexdigest(),
            'appliesTo': [{'path': 'work.cc', 'checksum': hashlib.sha256(target.read_bytes()).hexdigest()}]}))
        self.git(self.core, 'add', 'patches')
        self.git(self.core, 'commit', '-qm', 'patch inputs')
        self.src.joinpath('chrome').mkdir()
        version = self.src / 'chrome/VERSION'
        version.write_text('upstream\n')
        self.git(self.src, 'add', 'chrome/VERSION')
        self.git(self.src, 'commit', '-qm', 'version')
        version.write_text('my experiment\n')
        ctx = SimpleNamespace(log=None, state_root=Path(self.directory.name))
        model = sync_model.SyncModel(set(), chromium='skipped')
        conflicts = sync.local_work_conflicts(ctx, self.identity, model)
        self.assertIn('chrome/VERSION', {item['path'] for item in conflicts})
        self.assertEqual(version.read_text(), 'my experiment\n')

    def test_edited_patch_input_can_reapply_its_unchanged_recorded_output(self):
        # Editing a patch is a Core input change, not disposable Core output.
        # Its old materialized bytes remain identified by the patch metadata.
        import json
        from scaffold.brave import sync, sync_model
        self.core.joinpath('patches').mkdir()
        target = self.src / 'work.cc'
        patchfile = self.core / 'patches/work.patch'
        patchfile.write_text('diff --git a/work.cc b/work.cc\n--- a/work.cc\n+++ b/work.cc\n-old\n+new\n')
        patchfile.with_suffix('.patchinfo').write_text(json.dumps({'schemaVersion': 1,
            'patchChecksum': hashlib.sha256(patchfile.read_bytes()).hexdigest(),
            'appliesTo': [{'path': 'work.cc', 'checksum': hashlib.sha256(b'patched\n').hexdigest()}]}))
        self.git(self.core, 'add', 'patches')
        self.git(self.core, 'commit', '-qm', 'patch input')
        target.write_text('patched\n')
        patchfile.write_text(patchfile.read_text().replace('+new', '+changed patch'))
        ctx = SimpleNamespace(log=None, state_root=Path(self.directory.name))
        model = sync_model.SyncModel({self.src}, chromium='required')
        self.assertEqual(sync.local_work_conflicts(ctx, self.identity, model), [])
        self.assertIn('+changed patch', patchfile.read_text())
        target.write_text('patched and then edited\n')
        self.assertIn('work.cc', {item['path'] for item in sync.local_work_conflicts(ctx, self.identity, model)})

    def test_sync_dispatch_preserves_core_work_and_does_not_adopt_it(self):
        import json
        from unittest.mock import MagicMock, patch
        from scaffold.brave import sync, sync_model
        self.core.joinpath('patches').mkdir()
        patchfile = self.core / 'patches/work.patch'
        patchfile.write_text('diff --git a/work.cc b/work.cc\n--- a/work.cc\n+++ b/work.cc\n-old\n+new\n')
        patchfile.with_suffix('.patchinfo').write_text(json.dumps({'schemaVersion': 1,
            'patchChecksum': hashlib.sha256(patchfile.read_bytes()).hexdigest(),
            'appliesTo': [{'path': 'work.cc', 'checksum': hashlib.sha256(b'upstream\n').hexdigest()}]}))
        self.git(self.core, 'add', 'patches')
        self.git(self.core, 'commit', '-qm', 'patch input')
        target = self.core / 'work.cc'
        target.write_text('staged\n')
        self.git(self.core, 'add', 'work.cc')
        target.write_text('working\n')
        ctx = SimpleNamespace(log=None, state_root=Path(self.directory.name))
        execution = SimpleNamespace(identity=self.identity, toolchain=SimpleNamespace(
            manager='npm', node=Path('/node'), manager_entry=Path('/npm')), environ={})
        model = sync_model.SyncModel(set(), chromium='skipped')
        with patch.object(sync_model, 'inspect', return_value=model) as inspection, \
                patch.object(sync.packages, 'run', return_value=(['package', 'run', 'sync', '--nohooks'], 0)) as child:
            result = sync.do_sync_phase(ctx, execution, MagicMock(), 'mac', ['--nohooks'])
        self.assertEqual(child.call_count, 1)
        self.assertEqual(inspection.call_args.args[2], ['run', 'sync', '--nohooks'])
        self.assertEqual(result['scope']['chromium_sync'], 'skipped')
        self.assertEqual(target.read_text(), 'working\n')
        self.assertEqual(self.git(self.core, 'show', ':work.cc').stdout, 'staged\n')
        self.assertEqual(sync_scope.read_baseline(self.identity, Path(self.directory.name)), {})
