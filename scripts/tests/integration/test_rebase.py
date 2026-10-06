# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Rebase behavior against disposable local repositories, without browser builds."""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import SandboxTest
from scaffold.common.procs import CommandLog
from scaffold.common.results import Cancelled, ScaffoldError
from scaffold.brave import cmd_rebase
from tests.schema_validation import Validator


class RebaseTests(SandboxTest):
    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.core), *args], text=True, stderr=subprocess.PIPE).strip()

    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout(git=True)
        self.sandbox.commit_all('main')
        self.sandbox.register()
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('branch', '-M', 'master')
        self.base = self.git('rev-parse', 'HEAD')
        self.remote = self.sandbox.root / 'remote.git'
        subprocess.run(['git', 'clone', '--bare', str(self.core), str(self.remote)], check=True, capture_output=True)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('switch', '-c', 'topic')

    def commit(self, file, text):
        (self.core / file).write_text(text)
        self.git('add', file)
        self.git('commit', '-m', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def advance_remote(self, file='upstream.txt'):
        self.git('switch', 'master')
        head = self.commit(file, 'upstream\n')
        self.git('push', 'origin', 'master')
        self.git('switch', 'topic')
        return head

    def invoke(self):
        return self.sandbox.bcore_json('rebase', '--config', str(self.sandbox.config), cwd=self.core)

    def test_clean_rebase_fetches_and_keeps_other_branch_refs(self):
        original = self.commit('topic.txt', 'topic\n')
        self.git('branch', 'other-topic')
        self.git('config', 'rebase.updateRefs', 'true')
        self.git('config', 'rebase.autoStash', 'true')
        upstream = self.advance_remote()
        result, document = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Validator().problems(document), [])
        self.assertEqual(self.git('rev-parse', 'HEAD^'), upstream)
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), original)
        self.assertEqual(self.git('rev-parse', 'other-topic'), original)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_conflict_aborts_and_restores_branch_and_files(self):
        original = self.commit('same.txt', 'topic\n')
        self.advance_remote('same.txt')
        result, document = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CONFLICT', result.stderr)
        self.assertIn('Restored the original branch state', document['error']['message'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), original)
        self.assertEqual(self.git('branch', '--show-current'), 'topic')
        self.assertEqual((self.core / 'same.txt').read_text(), 'topic\n')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse((self.core / '.git' / 'rebase-merge').exists())

    def test_dirty_checkout_refused_before_fetch(self):
        self.advance_remote()
        for staged in (False, True):
            with self.subTest(staged=staged):
                (self.core / 'new.txt').write_text('keep me')
                if staged:
                    self.git('add', 'new.txt')
                before = self.git('status', '--porcelain')
                result, document = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('clean checkout', document['error']['message'])
                self.assertEqual(self.git('status', '--porcelain'), before)
                self.assertEqual((self.core / 'new.txt').read_text(), 'keep me')
                self.assertFalse((self.core / '.git' / 'FETCH_HEAD').exists())

    def test_fetch_failure_and_existing_operation_leave_head_unchanged(self):
        self.git('remote', 'set-url', 'origin', str(self.remote / 'missing'))
        result, document = self.invoke()
        self.assertIn('Fetch failed', document['error']['message'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        (self.core / '.git' / 'MERGE_HEAD').write_text(self.base + '\n')
        result, document = self.invoke()
        self.assertIn('existing Git operation', document['error']['message'])
        self.assertTrue((self.core / '.git' / 'MERGE_HEAD').exists())

    def test_merge_commits_are_refused(self):
        self.commit('topic.txt', 'topic')
        self.git('switch', '-c', 'side', self.base)
        self.commit('side.txt', 'side')
        self.git('switch', 'topic')
        self.git('merge', '--no-edit', 'side')
        original = self.git('rev-parse', 'HEAD')
        result, document = self.invoke()
        self.assertIn('merge commits', document['error']['message'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), original)

    def test_detached_head_is_refused_before_fetch(self):
        self.git('checkout', '--detach')
        result, document = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.core / '.git' / 'FETCH_HEAD').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)

    def test_interrupted_rebase_restores_and_abort_failure_is_reported(self):
        original = self.commit('same.txt', 'topic\n')
        self.advance_remote('same.txt')
        identity = SimpleNamespace(core=self.core, src=self.core.parent, record=None)
        ctx = SimpleNamespace(identity=lambda: identity, log=CommandLog(enabled=False),
                              json_mode=True, state_root=self.sandbox.config.parent)
        real_run = cmd_rebase.run_streaming

        def interrupted(argv, *args, **kwargs):
            code = real_run(argv, *args, **kwargs)
            if '--no-autostash' in argv:
                raise Cancelled(130)
            return code

        with patch.object(cmd_rebase, 'run_streaming', side_effect=interrupted):
            with self.assertRaises(Cancelled):
                cmd_rebase.run_rebase(ctx)
        self.assertEqual(self.git('rev-parse', 'HEAD'), original)
        self.assertEqual(self.git('status', '--porcelain'), '')

        def abort_fails(argv, *args, **kwargs):
            return 1 if '--abort' in argv else real_run(argv, *args, **kwargs)

        with patch.object(cmd_rebase, 'run_streaming', side_effect=abort_fails):
            with self.assertRaisesRegex(ScaffoldError, 'automatic abort failed'):
                cmd_rebase.run_rebase(ctx)
        self.assertTrue((self.core / '.git' / 'rebase-merge').exists())
        self.git('rebase', '--abort')
