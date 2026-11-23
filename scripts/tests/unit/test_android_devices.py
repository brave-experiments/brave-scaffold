# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Device choice, terminal prompts, and configured defaults."""

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.support
from scaffold.brave import adb, android
from scaffold.common.config import load_config
from scaffold.common.results import ScaffoldError

DEVICES = [{'id': 'emulator-5554', 'state': 'device'}, {'id': 'phone', 'state': 'device'}]
TWO_PHONES = [*DEVICES, {'id': 'tablet', 'state': 'device'}]


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

    def preflight(self, answers, terminal=True, deployment=False, devices=DEVICES):
        stdin, stderr = io.StringIO(answers), io.StringIO()
        stdin.isatty = lambda: terminal
        stderr.isatty = lambda: terminal
        with patch.object(android.sys, 'stdin', stdin), patch.object(android.sys, 'stderr', stderr), \
                patch.object(adb, 'require_adb', return_value='adb'), \
                patch.object(adb, 'list_devices', return_value=devices), \
                patch.object(adb, 'device_label', side_effect=lambda *args: args[1]['id']):
            outcome = android.preflight_deployment(self.ctx, 'arm64') if deployment else android.preflight_device(self.ctx)
        return outcome, stderr.getvalue()

    def test_picker_retries_and_never_offers_to_save(self):
        outcome, text = self.preflight('wrong\n3\n2\n')
        self.assertEqual(outcome, ('adb', DEVICES[1], 'interactive'))
        self.assertIn('1. emulator-5554', text)
        self.assertIn('2. phone', text)
        self.assertIn('Enter a number', text)
        self.assertNotIn('Remember', text)
        self.assertEqual(self.path.read_text(), self.original)

    def test_enter_selects_first_and_eof_cancels(self):
        outcome, text = self.preflight('\n')
        self.assertEqual(outcome[1], DEVICES[0])
        self.assertIn('Enter for 1', text)
        with self.assertRaises(ScaffoldError) as caught:
            self.preflight('')
        self.assertEqual(caught.exception.code, 'INVALID_INPUT')
        self.assertEqual(self.path.read_text(), self.original)

    def test_all_choice_uses_compatibility_checks(self):
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
        outcome, text = self.preflight('a\n2\n')
        self.assertEqual(outcome[1], DEVICES[1])
        self.assertNotIn('a. All', text)

    def test_json_plan_and_nonterminal_never_prompt(self):
        for json_mode, plan, terminal in ((True, False, True), (False, True, True), (False, False, False)):
            self.ctx.json_mode = json_mode
            self.values['plan'] = plan
            with self.assertRaises(ScaffoldError) as caught:
                self.preflight('2\n', terminal)
            self.assertEqual(caught.exception.code, 'DEVICE_AMBIGUOUS')
        self.assertEqual(self.path.read_text(), self.original)

    def test_flag_and_saved_default_skip_picker(self):
        for flag, default in (('phone', None), (None, 'phone')):
            self.values['device'] = flag
            self.ctx.config.default_android_device = default
            outcome, text = self.preflight('')
            self.assertEqual(outcome[1]['id'], 'phone')
            self.assertEqual(text, '')

    def test_physical_uses_the_only_physical_device_over_the_default(self):
        self.values['physical'] = True
        self.ctx.config.default_android_device = 'emulator-5554'
        outcome, text = self.preflight('')
        self.assertEqual(outcome, ('adb', DEVICES[1], 'physical'))
        self.assertEqual(text, '')

    def test_physical_without_a_usable_physical_device_is_unavailable(self):
        self.values['physical'] = True
        with self.assertRaises(ScaffoldError) as caught:
            self.preflight('', devices=[DEVICES[0], {'id': 'phone', 'state': 'unauthorized'}])
        self.assertEqual(caught.exception.code, 'DEVICE_UNAVAILABLE')
        self.assertIn('physical', caught.exception.message)
        self.assertEqual(caught.exception.details['devices'], ['phone (unauthorized)'])

    def test_several_physical_devices_pick_among_them_only(self):
        self.values['physical'] = True
        with self.assertRaises(ScaffoldError) as caught:
            self.preflight('', terminal=False, devices=TWO_PHONES)
        self.assertEqual(caught.exception.code, 'DEVICE_AMBIGUOUS')
        self.assertEqual(caught.exception.details['device_options'], [['--device', 'phone'], ['--device', 'tablet']])
        outcome, text = self.preflight('2\n', devices=TWO_PHONES)
        self.assertEqual(outcome[1]['id'], 'tablet')
        self.assertNotIn('emulator-5554', text)

    def test_physical_conflicts_with_other_selectors(self):
        for key, value in (('device', 'phone'), ('all_devices', True)):
            self.values.clear()
            self.values.update({'physical': True, key: value})
            with self.assertRaises(ScaffoldError) as caught:
                self.preflight('')
            self.assertEqual(caught.exception.code, 'SELECTOR_CONFLICT')
