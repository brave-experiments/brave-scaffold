# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Only reviewed support script bytes can select a complete write manifest."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.android_fixtures import APPLY_SCRIPT, COPY_SCRIPT, script_contracts
from scaffold.brave import support_scripts
from scaffold.common.results import ScaffoldError


class ScriptContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "contracts.json"
        self.manifest.write_text(json.dumps(script_contracts()))
        self.patch = mock.patch.object(support_scripts, "MANIFEST", self.manifest)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.root / "applyPatches.sh").write_text(APPLY_SCRIPT)
        (self.root / "copyMacRes.sh").write_text(COPY_SCRIPT)

    def test_reviewed_bytes_select_their_explicit_complete_manifest(self):
        contracts = support_scripts.require_contracts(self.root)
        self.assertEqual(contracts["copyMacRes.sh"]["resources"], [["third_party/jdk", "res/jdk/current"]])
        self.assertIn("SUPPORT_PATCHED", contracts["applyPatches.sh"]["direct"])

    def test_direct_and_indirect_code_changes_do_not_select_a_manifest(self):
        for suffix in ('echo changed > ../src/local.cc\n', 'source ./helper.sh\n'):
            with self.subTest(suffix=suffix):
                (self.root / "applyPatches.sh").write_text(APPLY_SCRIPT + suffix)
                with self.assertRaises(ScaffoldError) as caught:
                    support_scripts.require_contracts(self.root)
                self.assertEqual(caught.exception.code, "PREPARATION_CONFLICT")
                self.assertIn("applyPatches.sh", caught.exception.message)

    def test_a_script_cannot_approve_itself_with_a_local_manifest(self):
        (self.root / "applyPatches.sh").write_text("arbitrary shell code\n")
        (self.root / "support_script_contracts.json").write_text(json.dumps(script_contracts()))
        with self.assertRaises(ScaffoldError):
            support_scripts.require_contracts(self.root)

    def test_a_linked_script_is_not_an_approved_implementation(self):
        (self.root / "applyPatches.sh").rename(self.root / "outside.sh")
        (self.root / "applyPatches.sh").symlink_to(self.root / "outside.sh")
        with self.assertRaises(ScaffoldError):
            support_scripts.require_contracts(self.root)
