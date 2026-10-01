# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Terminal messages explain unknown origins and keep full structured evidence."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.support
from scaffold.brave import android_deps, doctor, rbe_checks
from scaffold.common.checks import WARNING, make_check
from scaffold.common.results import Result, emit, render_error_text


class MessageTests(unittest.TestCase):
    def test_unknown_resource_origin_is_not_called_a_user_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'support/res/config.star'
            copied = root / 'src/build/config/config.star'
            for path, content in ((source, 'support\n'), (copied, 'unknown\n')):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            identity = SimpleNamespace(src=root / 'src')
            with patch.object(android_deps, 'resource_manifest', return_value=[('build/config', 'res/config.star')]):
                current, reason = android_deps.resources_current(identity, root / 'support')
            self.assertFalse(current)
            self.assertIn('build/config/config.star', reason)
            self.assertIn('origin unknown', reason)
            self.assertNotIn('local edits', reason)
            self.assertEqual(copied.read_text(), 'unknown\n')
            receipt = {'resources': {'build/config/config.star': android_deps.resource_signature(source, copied)},
                       'resource_sources': {'build/config/config.star': android_deps._sha(source)}}
            copied.write_text('changed since copy\n')
            with patch.object(android_deps, 'resource_manifest', return_value=[('build/config', 'res/config.star')]):
                current, reason = android_deps.resources_current(identity, root / 'support', receipt)
            self.assertFalse(current)
            self.assertIn('changed since the last recorded copy', reason)
            self.assertNotIn('origin unknown', reason)

    def test_display_label_does_not_replace_json_identifier(self):
        check = make_check('android-support-currency', WARNING, 'Refresh needed.', 'android',
                           required=False, affects=('android build',))
        text = doctor.render_text(['android'], [check])
        self.assertIn('Android support files', text)
        self.assertIn('affects android build', text)
        self.assertNotIn('optional', text)
        self.assertNotIn('android-support-currency', text)
        self.assertEqual(check.to_dict()['name'], 'android-support-currency')

    def test_large_evidence_is_bounded_and_json_keeps_every_file(self):
        files = [{'path': 'file%d.cc' % i, 'reason': 'origin unknown'} for i in range(80)]
        error = {'code': 'PREPARATION_CONFLICT', 'message': 'Build blocked.',
                 'details': {'files': files, 'path_base': '/fixture/src', 'scope': {'writes': files}},
                 'repairs': []}
        result = Result(command='build', error=error)
        stdout, stderr = io.StringIO(), io.StringIO()
        emit(result, False, stdout, stderr)
        text = stderr.getvalue()
        self.assertIn('file0.cc: origin unknown', text)
        self.assertIn('path base: /fixture/src', text)
        self.assertIn('--json', text)
        self.assertLess(len(text.splitlines()), 16)
        self.assertNotIn('{"', text)
        emit(result, True, stdout, io.StringIO())
        self.assertEqual(json.loads(stdout.getvalue())['error']['details']['files'], files)

    def test_recovery_commands_keep_working_directory_and_appear_once(self):
        step = {'argv': ['git', 'status', '--short'], 'cwd': '/fixture/a b'}
        text = render_error_text({'code': 'PREPARATION_CONFLICT', 'message': 'Inspect files.',
                                  'repairs': [step, step]})
        self.assertIn("cd '/fixture/a b' && git status --short", text)
        self.assertEqual(text.count('Next:'), 1)

    def test_support_recovery_matches_the_blocker(self):
        identity = SimpleNamespace(core=Path('/fixture/src/brave'))
        skip = android_deps.SupportPlan('conflict', 'refresh disabled', conflicts=[
            {'path': 'support', 'reason': 'refresh is needed but --skip-support-refresh was set'}])
        unknown = android_deps.SupportPlan('conflict', 'unknown scope', conflicts=[
            {'path': 'support', 'reason': 'incomplete evidence'}])
        retry = android_deps.conflict_error(skip, identity).repairs[0]
        inspect = android_deps.conflict_error(unknown, identity).repairs[0]
        self.assertIn('build', retry['argv'])
        self.assertIn('--skip-support-refresh', retry['note'])
        self.assertIn('doctor', inspect['argv'])
        self.assertNotIn('build', inspect['argv'])


class ProbeMessageTests(unittest.TestCase):
    def test_sdk_probe_failure_does_not_claim_sdk_is_visible(self):
        failed = SimpleNamespace(returncode=1, stdout='unusable SDK', stderr='failure')
        ctx = SimpleNamespace(environ={}, log=None)
        with patch.object(rbe_checks, 'run_capture', return_value=failed), \
                patch.object(rbe_checks, 'METAL_MOUNTS', '/fixture/missing'):
            sdk = rbe_checks._machine(ctx, 'mac')[0]
        self.assertEqual(sdk.status, 'blocker')
        self.assertIn('not visible', sdk.summary)
        self.assertTrue(sdk.repairs)

    def test_nested_readiness_error_shows_each_problem_on_one_line(self):
        checks = [make_check('local-tools', 'blocker', 'Node is missing.', 'mac').to_dict()]
        text = render_error_text({'code': 'LOCAL_TOOL_MISSING', 'message': 'Tools are not ready.',
                                  'details': {'checks': checks, 'blocking': ['local-tools']}})
        self.assertIn('Checkout tools [blocker]: Node is missing.', text)
        self.assertIn('blocking: Checkout tools', text)
        self.assertNotIn('local-tools', text)
        self.assertNotIn('{"', text)
