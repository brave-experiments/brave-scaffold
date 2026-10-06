# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Status preserves the distinction between operation history and current source."""

import json
import subprocess

from tests.support import SandboxTest, tree_snapshot
from tests.schema_validation import Validator
from scaffold.brave import records


class StatusTests(SandboxTest):
    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout(git=True)
        self.sandbox.commit_all('main')
        self.sandbox.register()
        self.head = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.core), *args], text=True)

    def record(self, name, **overrides):
        data = {'operation_id': name, 'command': 'test', 'checkout': str(self.core),
                'state': 'complete', 'status': 'ok', 'finished': '2026-10-06T10:00:00+1000',
                'source': {'core_branch': self.git('branch', '--show-current').strip(), 'core_head': self.head, 'core_uncommitted_files': 1},
                'details': {'target': 'mac', 'suite': 'brave_unit_tests'}}
        data.update(overrides)
        path = self.sandbox.config.parent / '.bcore' / 'operations' / (name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def status(self, *args):
        result, document = self.sandbox.bcore_json('status', *args, '--config', str(self.sandbox.config), cwd=self.core)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Validator().problems(document), [])
        return document['data']

    def test_read_only_without_environment_and_no_history(self):
        self.git('update-ref', 'refs/remotes/origin/master', self.head)
        (self.core / 'new test.cc').write_text('test')
        self.git('add', 'new test.cc')
        (self.core / 'new test.cc').write_text('modified')
        (self.core / 'untracked.cc').write_text('new')
        before = tree_snapshot(self.core)
        data = self.status()
        self.assertEqual(data['change_counts'], {'staged': 1, 'unstaged': 1, 'untracked': 1})
        self.assertEqual(data['base'], {'ref': 'origin/master', 'ahead': 0, 'behind': 0})
        self.assertEqual(data['history'], [])
        self.assertGreater(data['disk_free_bytes'], 0)
        self.assertEqual(before, tree_snapshot(self.core))
        self.assertEqual(self.sandbox.records(), [])

    def test_branch_switch_and_same_head_edits_never_claim_verification(self):
        self.record('old')
        self.git('switch', '-c', 'different-branch')
        (self.core / 'changed.cc').write_text('a')
        data = self.status()
        self.assertEqual(data['branch'], 'different-branch')
        self.assertEqual(data['history'], [])
        self.assertTrue(self.status('--all-branches')['history'][0]['same_head'])
        self.record('current')
        self.assertEqual(data['current_test_verification'], 'unknown')
        (self.core / 'changed.cc').write_text('b')
        self.sandbox.commit_all('main')
        data = self.status()
        self.assertFalse(data['history'][0]['same_head'])
        self.assertEqual(data['history'][0]['verification'], 'unknown')
        self.git('checkout', '--detach', self.head)
        self.assertIsNone(self.status()['branch'])
        self.assertEqual(self.status()['history'], [])
        self.assertEqual(len(self.status('--all-branches')['history']), 2)

    def test_latest_failure_offsets_checkout_isolation_and_unfinished(self):
        self.record('a-success')
        self.record('b-failure', status='error', finished='2026-10-06T01:00:00+0000')
        self.record('c-other', checkout='/another/checkout', finished='2026-10-07T01:00:00+0000')
        self.record('d-incomplete', state='incomplete', started='2026-10-06T11:01:00+1000')
        folder = self.sandbox.config.parent / '.bcore' / 'outputs' / records.checkout_key(self.core)
        folder.mkdir(parents=True)
        (folder / 'output.json').write_text(json.dumps({'output_dir': '/out/mac', 'needs_revalidation': True}))
        data = self.status()
        self.assertEqual([r['operation_id'] for r in data['history']], ['b-failure'])
        self.assertEqual(data['history'][0]['status'], 'error')
        self.assertEqual(data['incomplete_operations'][0]['process_state'], 'unknown')
        self.assertTrue(data['outputs'][0]['needs_revalidation'])
        result = self.sandbox.bcore('status', '--config', str(self.sandbox.config), cwd=self.core)
        self.assertIn('failed', result.stdout)
        self.assertIn('history, not current verification', result.stdout)
        self.assertIn('process state unknown', result.stdout)

    def test_unknown_branch_is_only_shown_with_all_branches(self):
        self.record('legacy', source={'core_head': self.head})
        self.record('current')
        self.assertEqual([r['operation_id'] for r in self.status()['history']], ['current'])
        history = self.status('--all-branches')['history']
        self.assertEqual({r['operation_id'] for r in history}, {'legacy', 'current'})
        result = self.sandbox.bcore('status', '--all-branches', '--config', str(self.sandbox.config), cwd=self.core)
        self.assertIn('branch unknown', result.stdout)
