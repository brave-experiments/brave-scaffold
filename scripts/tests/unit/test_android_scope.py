# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Only reviewed support script bytes can select a complete write manifest."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from tests.android_fixtures import APPLY_SCRIPT, COPY_SCRIPT, script_contracts
from scaffold.brave import android_deps, support_scripts
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


class CheckoutResourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.identity = SimpleNamespace(src=self.root / "src")
        self.source = self.identity.src / "third_party/jdk/current/Contents/Home/bin"
        self.destination = self.identity.src / "third_party/jdk/current/bin"
        self.source.mkdir(parents=True)
        (self.source / "java.chromium").write_text("synced java")
        self.alias = self.identity.src / "third_party/jdk/current/lib/libjava.so"
        contract = {"resources": [], "checkout_resources": [
            ["third_party/jdk/current/Contents/Home/bin", "third_party/jdk/current/bin"]],
            "required_resources": ["third_party/jdk/current/lib/libjava.so"]}
        patcher = mock.patch.object(support_scripts, "require_contract", return_value=contract)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_copy_roll_and_alias_require_refresh(self):
        current = lambda: android_deps.resources_current(self.identity, self.root / "support")[0]
        self.assertFalse(current())
        shutil.copytree(self.source, self.destination)
        self.assertFalse(current())
        self.alias.parent.mkdir()
        self.alias.touch()
        self.assertTrue(current())
        source_time = 1_790_000_000_349_327_218
        os.utime(self.source / "java.chromium", ns=(source_time, source_time))
        copied_time = source_time // 1_000_000_000 * 1_000_000_000
        os.utime(self.destination / "java.chromium", ns=(copied_time, copied_time))
        self.assertTrue(current())
        (self.source / "java.chromium").write_text("new synced java version")
        self.assertFalse(current())
        shutil.copy2(self.source / "java.chromium", self.destination / "java.chromium")
        self.assertTrue(current())
        self.alias.unlink()
        self.assertFalse(current())

    def test_inventory_names_only_copied_entries_and_blocks_link_escape(self):
        destinations = android_deps.resource_destinations(self.identity, self.root / "support")
        self.assertEqual([item[0] for item in destinations], ["third_party/jdk/current/bin"])
        self.assertEqual(destinations[0][1], self.source)
        self.destination.symlink_to(self.root)
        with self.assertRaises(ScaffoldError) as caught:
            android_deps.resource_destinations(self.identity, self.root / "support")
        self.assertEqual(caught.exception.code, "PREPARATION_CONFLICT")


class NoLargeFilesTests(unittest.TestCase):
    def test_setup_skips_lfs_commands_and_storage_without_lfs_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitattributes").write_text("patches/*.patch whitespace=-blank-at-eof\n")
            store = root / ".git/lfs"
            with mock.patch.object(android_deps, "_git") as git:
                android_deps.materialize_lfs(root, root, store)
                git.assert_not_called()
            self.assertFalse(store.exists())
