# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Approval and backups protect real Git files before the sync child starts."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.unit.test_sync_preservation import SyncFixture
from scaffold.brave import records, sync, sync_model, sync_overwrite
from scaffold.common.cli import Parsed
from scaffold.common.results import ScaffoldError


class Terminal(io.StringIO):
    def isatty(self):
        return True


class SyncOverwriteTests(SyncFixture):
    def setUp(self):
        super().setUp()
        patchfile = self.core / 'patches/work.patch'
        patchfile.parent.mkdir()
        patchfile.write_text('diff --git a/work.cc b/work.cc\n--- a/work.cc\n+++ b/work.cc\n-old\n+new\n')
        import hashlib
        patchfile.with_suffix('.patchinfo').write_text(json.dumps({'schemaVersion': 1,
            'patchChecksum': hashlib.sha256(patchfile.read_bytes()).hexdigest(),
            'appliesTo': [{'path': 'work.cc', 'checksum': hashlib.sha256(b'upstream\n').hexdigest()}]}))
        self.git(self.core, 'add', 'patches')
        self.git(self.core, 'commit', '-qm', 'patch input')
        self.target = self.src / 'work.cc'
        self.target.write_text('staged work\n')
        self.git(self.src, 'add', 'work.cc')
        self.target.write_text('working work\n')
        self.ctx = SimpleNamespace(log=None, state_root=Path(self.directory.name),
                                   parsed=Parsed(values={'overwrite_local_changes': True}),
                                   identity=lambda: self.identity)
        self.execution = SimpleNamespace(identity=self.identity, toolchain=SimpleNamespace(
            manager='npm', node=Path('/node'), manager_entry=Path('/npm')), environ={})
        self.model = sync_model.SyncModel({self.src}, chromium='required')
        self.op = records.Operation('sync', self.identity, {}, root=self.ctx.state_root)

    def run_sync(self, code=0):
        def child(*args):
            self.assertEqual(self.target.read_text(), 'upstream\n')
            self.assertEqual(self.git(self.src, 'diff', '--cached').stdout, '')
            return ['package', 'run', 'sync'], code
        with patch.object(sync_model, 'inspect', return_value=self.model), \
                patch.object(sync.packages, 'run', side_effect=child) as dispatched:
            result = sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
        self.assertEqual(dispatched.call_count, 1)
        return result

    def backup_file(self):
        return next((self.op.root / 'backups').glob('*/sync-overwrite/0/worktree/work.cc'))

    def test_explicit_approval_backs_up_worktree_and_index_and_preserves_unrelated_files(self):
        unrelated = self.core / 'notes.txt'
        unrelated.write_text('keep me\n')
        result = self.run_sync()
        self.assertEqual(self.backup_file().read_text(), 'working work\n')
        backup = Path(result['overwrite_backup'])
        self.assertIn('staged work', (backup / '0/staged.patch').read_text())
        self.assertEqual(unrelated.read_text(), 'keep me\n')
        # Prove recovery of staged bytes independently by applying the saved patch.
        self.git(self.src, 'apply', '--cached', str(backup / '0/staged.patch'))
        self.assertEqual(self.git(self.src, 'show', ':work.cc').stdout, 'staged work\n')

    def test_backup_survives_child_failure(self):
        with self.assertRaises(ScaffoldError) as caught:
            self.run_sync(code=9)
        self.assertEqual(caught.exception.code, 'CHILD_FAILED')
        self.assertEqual(self.backup_file().read_text(), 'working work\n')

    def test_no_and_eof_keep_files_and_index_and_do_not_dispatch(self):
        self.ctx.parsed = Parsed()
        for answer in ('n\n', '\n', ''):
            with self.subTest(answer=answer), patch.object(sync_overwrite.sys, 'stdin', Terminal(answer)), \
                    patch.object(sync_overwrite.sys, 'stderr', Terminal()), \
                    patch.object(sync_model, 'inspect', return_value=self.model), \
                    patch.object(sync.packages, 'run') as child:
                with self.assertRaises(ScaffoldError):
                    sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
                child.assert_not_called()
                self.assertEqual(self.target.read_text(), 'working work\n')
                self.assertEqual(self.git(self.src, 'show', ':work.cc').stdout, 'staged work\n')
                self.assertFalse((self.op.root / 'backups').exists())

    def test_show_diffs_then_yes_runs_sync_and_lists_each_file_once(self):
        self.ctx.parsed = Parsed()
        output = Terminal()
        with patch.object(sync_overwrite.sys, 'stdin', Terminal('d\ny\n')), \
                patch.object(sync_overwrite.sys, 'stderr', output):
            self.run_sync()
        text = output.getvalue()
        self.assertIn('Overwrite these changes? [y/N]', text)
        self.assertIn('working work', text)
        self.assertIn('staged work', text)
        self.assertEqual(text.count('"path": "work.cc"'), 1)

    def test_edit_during_confirmation_requires_new_review(self):
        def confirm(*args):
            self.target.write_text('new edit\n')
            return True
        with patch.object(sync_overwrite, 'confirm', side_effect=confirm), \
                patch.object(sync_model, 'inspect', return_value=self.model), \
                patch.object(sync.packages, 'run') as child:
            with self.assertRaises(ScaffoldError) as caught:
                sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
            self.assertIn('changed during review', caught.exception.message)
            child.assert_not_called()
        self.assertEqual(self.target.read_text(), 'new edit\n')
        self.assertFalse((self.op.root / 'backups').exists())

    def test_changed_sync_scope_requires_new_review(self):
        other = sync_model.SyncModel({self.src}, chromium='required', writes={self.core: 'new hook'})
        with patch.object(sync_model, 'inspect', side_effect=[self.model, other]), \
                patch.object(sync.packages, 'run') as child:
            with self.assertRaises(ScaffoldError) as caught:
                sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
            self.assertIn('scope changed', caught.exception.message)
            child.assert_not_called()
        self.assertEqual(self.target.read_text(), 'working work\n')

    def test_noninteractive_without_flag_never_reads_input(self):
        self.ctx.parsed = Parsed()
        with patch.object(sync_overwrite.sys.stdin, 'isatty', return_value=False), \
                patch.object(sync_overwrite.sys.stdin, 'readline', side_effect=AssertionError('must not prompt')), \
                patch.object(sync_model, 'inspect', return_value=self.model):
            with self.assertRaises(ScaffoldError):
                sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
        self.assertFalse((self.op.root / 'backups').exists())

    def test_untracked_file_is_backed_up_before_removal(self):
        note = self.src / 'notes.txt'
        note.write_bytes(b'\x00\xffmy note')
        self.run_sync()
        self.assertFalse(note.exists())
        self.assertEqual((self.backup_file().parent / 'notes.txt').read_bytes(), b'\x00\xffmy note')

    def test_local_commits_cannot_be_overwritten(self):
        self.git(self.src, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git(self.src, 'remote', 'add', 'origin', '/unused')
        self.git(self.src, 'config', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
        self.git(self.src, 'branch', '--set-upstream-to=origin/main')
        self.git(self.src, 'commit', '-qm', 'local work')
        with patch.object(sync_model, 'inspect', return_value=self.model), patch.object(sync.packages, 'run') as child:
            with self.assertRaises(ScaffoldError):
                sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
            child.assert_not_called()

    def test_staged_addition_and_deletion_are_saved_before_restore(self):
        added = self.src / 'added.bin'
        added.write_bytes(b'\x00\xfforiginal')
        self.git(self.src, 'add', 'added.bin')
        removed = self.src / 'deleted.cc'
        removed.write_text('upstream file\n')
        self.git(self.src, 'add', 'deleted.cc')
        self.git(self.src, 'commit', '-qm', 'another upstream file', '--', 'deleted.cc')
        self.git(self.src, 'rm', 'deleted.cc')
        result = self.run_sync()
        backup = Path(result['overwrite_backup'])
        self.assertEqual((backup / '0/worktree/added.bin').read_bytes(), b'\x00\xfforiginal')
        self.assertFalse(added.exists())
        self.assertEqual(removed.read_text(), 'upstream file\n')
        self.git(self.src, 'apply', '--cached', str(backup / '0/staged.patch'))
        self.assertIn('added.bin', self.git(self.src, 'diff', '--cached', '--name-only').stdout)
        self.assertIn('deleted.cc', self.git(self.src, 'diff', '--cached', '--name-only').stdout)

    def test_staged_edit_during_review_stops_even_with_unchanged_working_bytes(self):
        def confirm(*args):
            self.target.write_text('new staged edit\n')
            self.git(self.src, 'add', 'work.cc')
            self.target.write_text('working work\n')
            return True
        with patch.object(sync_overwrite, 'confirm', side_effect=confirm), \
                patch.object(sync_model, 'inspect', return_value=self.model), \
                patch.object(sync.packages, 'run') as child:
            with self.assertRaises(ScaffoldError):
                sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
            child.assert_not_called()
        self.assertEqual(self.git(self.src, 'show', ':work.cc').stdout, 'new staged edit\n')
        self.assertEqual(self.target.read_text(), 'working work\n')

    def test_copy_failure_leaves_files_and_index_untouched(self):
        with patch.object(sync_overwrite.shutil, 'copy2', side_effect=OSError('disk full')), \
                patch.object(sync_model, 'inspect', return_value=self.model), \
                patch.object(sync.packages, 'run') as child:
            with self.assertRaises(OSError):
                sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
            child.assert_not_called()
        self.assertEqual(self.target.read_text(), 'working work\n')
        self.assertEqual(self.git(self.src, 'show', ':work.cc').stdout, 'staged work\n')

    def test_overwrite_option_is_consumed_by_all_sync_commands(self):
        from scaffold.brave.registry import REGISTRY
        from scaffold.common.cli import parse_tokens
        for name in ('sync', 'sync-build', 'sync-build-run', 'sb', 'sbr'):
            parsed = parse_tokens(REGISTRY[name], ['--overwrite-local-changes', '--nohooks'])
            self.assertTrue(parsed.get('overwrite_local_changes'))
            self.assertEqual(parsed.forwarded, ['--nohooks'])

    def test_approval_leaves_staged_work_outside_hook_write_scope_in_same_repository(self):
        unrelated = self.src / 'unrelated.cc'
        unrelated.write_text('keep staged\n')
        self.git(self.src, 'add', 'unrelated.cc')
        self.model = sync_model.SyncModel(set(), chromium='skipped', writes={self.target: 'reviewed hook'})
        with patch.object(sync_model, 'inspect', return_value=self.model), \
                patch.object(sync.packages, 'run', return_value=(['package', 'run', 'sync'], 0)) as child:
            sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
            child.assert_called_once()
        self.assertEqual(self.target.read_text(), 'upstream\n')
        self.assertEqual(unrelated.read_text(), 'keep staged\n')
        self.assertEqual(self.git(self.src, 'show', ':unrelated.cc').stdout, 'keep staged\n')
        self.assertEqual(self.git(self.src, 'diff', '--cached', '--name-only').stdout.strip(), 'unrelated.cc')

    def test_symlink_conflict_cannot_be_approved(self):
        self.target.unlink()
        self.target.symlink_to(self.core / 'work.cc')
        with patch.object(sync_model, 'inspect', return_value=self.model), patch.object(sync.packages, 'run') as child:
            with self.assertRaises(ScaffoldError):
                sync.do_sync_phase(self.ctx, self.execution, self.op, 'mac', [])
            child.assert_not_called()
        self.assertTrue(self.target.is_symlink())
        self.assertFalse((self.op.root / 'backups').exists())

    def test_restored_head_is_safe_when_patch_metadata_describes_different_bytes(self):
        import hashlib
        info = self.core / 'patches/work.patchinfo'
        document = json.loads(info.read_text())
        document['appliesTo'][0]['checksum'] = hashlib.sha256(b'previous patched output\n').hexdigest()
        info.write_text(json.dumps(document))
        self.git(self.core, 'add', 'patches/work.patchinfo')
        self.git(self.core, 'commit', '-qm', 'record patched output')
        self.run_sync()
        self.assertEqual(self.backup_file().read_text(), 'working work\n')
