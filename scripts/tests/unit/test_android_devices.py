# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Device choice, terminal prompts, and scaffold-owned defaults."""

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.support
from scaffold.brave import adb, android
from scaffold.common.config import load_config, save_android_device
from scaffold.common.results import ScaffoldError

DEVICES = [{'id': 'emulator-5554', 'state': 'device'}, {'id': 'phone', 'state': 'device'}]


class DevicePickerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'config.toml'
        self.original = 'schema_version = 1\n# keep this comment\n[defaults]\nplatform = "android"\n'
        self.path.write_text(self.original)
        self.values = {}
        self.ctx = SimpleNamespace(environ={}, log=None, json_mode=False,
                                   parsed=SimpleNamespace(get=self.values.get), config=load_config(self.path))

    def preflight(self, answers, terminal=True, deployment=False):
        stdin, stderr = io.StringIO(answers), io.StringIO()
        stdin.isatty = lambda: terminal
        stderr.isatty = lambda: terminal
        with patch.object(android.sys, 'stdin', stdin), patch.object(android.sys, 'stderr', stderr), \
                patch.object(adb, 'require_adb', return_value='adb'), \
                patch.object(adb, 'list_devices', return_value=DEVICES), \
                patch.object(adb, 'device_label', side_effect=lambda *args: args[1]['id']):
            outcome = android.preflight_deployment(self.ctx, 'arm64') if deployment else android.preflight_device(self.ctx)
        return outcome, stderr.getvalue()

    def test_picker_retries_and_saves_only_after_yes(self):
        outcome, text = self.preflight('wrong\n3\n2\ny\n')
        self.assertEqual(outcome, ('adb', DEVICES[1], 'interactive'))
        self.assertIn('1. emulator-5554', text)
        self.assertIn('2. phone', text)
        self.assertIn('Enter a number', text)
        self.assertEqual(load_config(self.path).default_android_device, 'phone')
        self.assertIn('# keep this comment', self.path.read_text())
        self.assertEqual(load_config(self.path).default_platform, 'android')

    def test_declining_or_eof_at_save_keeps_config_unchanged(self):
        for answer in ('1\nn\n', '1\n'):
            outcome, _ = self.preflight(answer)
            self.assertEqual(outcome[1]['id'], 'emulator-5554')
            self.assertEqual(self.path.read_text(), self.original)

    def test_enter_selects_first_and_eof_cancels(self):
        outcome, text = self.preflight('\n')
        self.assertEqual(outcome[1], DEVICES[0])
        self.assertIn('Enter for 1', text)
        with self.assertRaises(ScaffoldError) as caught:
            self.preflight('')
        self.assertEqual(caught.exception.code, 'INVALID_INPUT')
        self.assertEqual(self.path.read_text(), self.original)

    def test_all_choice_uses_compatibility_checks_and_does_not_save(self):
        for answer in ('a\n', 'A\n'):
            with patch.object(adb, 'device_capabilities', side_effect=lambda adapter, device, *args:
                              {**device, 'abis': ['arm64-v8a'] if device['id'] == 'phone' else ['x86_64'], 'sdk': 35}):
                outcome, text = self.preflight(answer, deployment=True)
            self.assertIsInstance(outcome, android.DeviceGroup)
            self.assertEqual([device['id'] for device in outcome.devices], ['phone'])
            self.assertEqual(outcome.skipped[0]['device'], 'emulator-5554')
            self.assertIn('a. All compatible devices', text)
            self.assertNotIn('Remember', text)
            self.assertEqual(self.path.read_text(), self.original)

    def test_single_device_selection_does_not_offer_all(self):
        outcome, text = self.preflight('a\n2\nn\n')
        self.assertEqual(outcome[1], DEVICES[1])
        self.assertNotIn('a. All', text)

    def test_json_plan_and_nonterminal_never_prompt(self):
        for json_mode, plan, terminal in ((True, False, True), (False, True, True), (False, False, False)):
            self.ctx.json_mode = json_mode
            self.values['plan'] = plan
            with self.assertRaises(ScaffoldError) as caught:
                self.preflight('2\ny\n', terminal)
            self.assertEqual(caught.exception.code, 'DEVICE_AMBIGUOUS')
        self.assertEqual(self.path.read_text(), self.original)

    def test_flag_and_saved_default_skip_picker(self):
        for flag, default in (('phone', None), (None, 'phone')):
            self.values['device'] = flag
            self.ctx.config.default_android_device = default
            outcome, text = self.preflight('')
            self.assertEqual(outcome[1]['id'], 'phone')
            self.assertEqual(text, '')

    def test_save_failure_still_uses_selected_device(self):
        with patch.object(android, 'save_android_device', side_effect=OSError('read only')):
            outcome, text = self.preflight('2\ny\n')
        self.assertEqual(outcome[1]['id'], 'phone')
        self.assertIn('Could not save', text)

    def test_missing_defaults_and_quoted_table_preserve_settings(self):
        for text in ('schema_version = 1\n[logging]\nverbosity = "quiet"\n',
                     'schema_version = 1\n["defaults"] # comment\nplatform = "mac"\n'):
            self.path.write_text(text)
            save_android_device(self.path, 'phone')
            config = load_config(self.path)
            self.assertEqual(config.default_android_device, 'phone')
            if '[logging]' in text:
                self.assertEqual(config.verbosity, 'quiet')
                self.assertIn(text.strip(), self.path.read_text())
            else:
                self.assertEqual(config.default_platform, 'mac')
                self.assertIn('["defaults"] # comment', self.path.read_text())

    def test_unsupported_layout_is_not_overwritten(self):
        original = 'schema_version = 1\ndefaults.platform = "android"\n'
        self.path.write_text(original)
        with self.assertRaises(ScaffoldError):
            save_android_device(self.path, 'phone')
        self.assertEqual(self.path.read_text(), original)
