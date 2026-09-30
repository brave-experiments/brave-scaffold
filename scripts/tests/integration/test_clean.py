# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cleanup preview, execution, and safety checks."""

import os
import shutil
import unittest
from pathlib import Path

from tests.support import SandboxTest, tree_snapshot
from scaffold.brave import clean

NAMES = ["Debug_arm64", "Debug_x64", "Release_arm64", "DebugOrigin_arm64", "Debug", "android_Debug_arm64",
         "android_tests_Debug_arm64", "android_Release_arm64", "Default", "Debugger"]


class CleanTests(SandboxTest):
    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout("main")
        self.out = self.core.parent / "out"
        for name in NAMES:
            (self.out / name / "obj").mkdir(parents=True)
            (self.out / name / "obj" / "x.o").write_text(name)
        self.sandbox.write_config([("main", self.core, None)])
        self.config = str(self.sandbox.config)

    def clean(self, *args):
        return self.sandbox.bdev_json("clean", "--checkout", "main", "--config", self.config, *args)

    def remaining(self):
        return sorted(p.name for p in self.out.iterdir())

    def names(self, document):
        return sorted(e["name"] for e in document["data"]["entries"])

    def test_preview_writes_nothing_and_defaults_to_the_host_target_only(self):
        before = tree_snapshot(self.core.parents[3])
        result, document = self.clean()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, tree_snapshot(self.core.parents[3]))
        self.assertEqual(self.names(document), ["Debug", "DebugOrigin_arm64", "Debug_arm64", "Debug_x64", "Release_arm64"])
        self.assertEqual(document["data"]["mode"], "preview")

    def test_execute_deletes_only_the_selected_directories(self):
        result, document = self.clean("android", "--configuration", "debug", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.names(document), ["android_Debug_arm64", "android_tests_Debug_arm64"])
        self.assertEqual(self.remaining(), sorted(set(NAMES) - {"android_Debug_arm64", "android_tests_Debug_arm64"}))

    def test_arch_narrows_the_match(self):
        self.clean("mac", "--arch", "arm64", "--execute")
        self.assertIn("Debug_x64", self.remaining())
        self.assertNotIn("Debug_arm64", self.remaining())
        self.assertNotIn("Debug", self.remaining())

    def test_all_is_explicit_and_omission_is_not_all(self):
        self.clean("--execute")
        self.assertIn("android_Debug_arm64", self.remaining())
        self.clean("all", "--execute")
        self.assertEqual(self.remaining(), ["Debugger", "Default"])

    def test_unknown_and_unavailable_targets_are_rejected_without_deleting(self):
        for token, code in (("nonsense", "INVALID_INPUT"), ("ios", "UNSUPPORTED_CAPABILITY")):
            result, document = self.clean(token, "--execute")
            self.assertEqual(document["error"]["code"], code)
        self.assertEqual(self.remaining(), sorted(NAMES))

    def test_symlinked_entries_are_skipped_and_their_targets_survive(self):
        victim = self.sandbox.root / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        shutil.rmtree(self.out / "Debug_arm64")
        (self.out / "Debug_arm64").symlink_to(victim)
        result, document = self.clean("mac", "--execute")
        self.assertEqual((result.returncode, document["status"]), (6, "partial"))
        outcomes = {e["name"]: e["outcome"] for e in document["data"]["entries"]}
        self.assertEqual(outcomes["Debug_arm64"], "skipped")
        self.assertEqual(outcomes["Release_arm64"], "deleted")
        self.assertTrue((victim / "keep").exists())

    def test_symlink_to_a_sibling_output_is_skipped_by_name_not_followed(self):
        shutil.rmtree(self.out / "Debug_arm64")
        (self.out / "Debug_arm64").symlink_to(self.out / "Default")
        result, document = self.clean("mac", "--configuration", "debug", "--arch", "arm64", "--execute")
        outcomes = {e["name"]: (e["outcome"], e["detail"]) for e in document["data"]["entries"]}
        self.assertEqual(outcomes["Debug_arm64"], ("skipped", "is a symlink"))
        self.assertTrue((self.out / "Default" / "obj" / "x.o").exists())

    def test_symlinked_out_directory_is_refused(self):
        real = self.core.parent / "elsewhere"
        self.out.rename(real)
        self.out.symlink_to(real)
        result, document = self.clean("mac", "--execute")
        self.assertEqual(document["error"]["code"], "OWNERSHIP_CONFLICT")
        self.assertTrue((real / "Debug_arm64").exists())

    def test_another_checkouts_output_is_never_touched(self):
        other = self.sandbox.make_checkout("other")
        (other.parent / "out" / "Debug_arm64").mkdir(parents=True)
        self.clean("all", "--execute")
        self.assertTrue((other.parent / "out" / "Debug_arm64").exists())

    def test_directories_with_git_metadata_are_skipped(self):
        (self.out / "Debug_x64" / ".git").mkdir()
        result, document = self.clean("mac", "--execute")
        self.assertEqual(document["status"], "partial")
        self.assertIn("Debug_x64", self.remaining())

    def test_no_size_omits_sizes(self):
        _, document = self.clean("--no-size")
        self.assertIsNone(document["data"]["total_kib"])
        self.assertTrue(all(e["size_kib"] is None for e in document["data"]["entries"]))


class RevalidationTests(SandboxTest):
    def test_entry_replaced_after_planning_is_rejected_not_followed(self):
        out = self.sandbox.root / "src" / "out"
        (out / "Debug_arm64").mkdir(parents=True)
        outside = self.sandbox.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x")
        entries = clean.plan_cleanup(out, ["mac"], ["debug"], None, False)
        self.assertEqual([e.outcome for e in entries], ["planned"])

        def swap(entry):
            shutil.rmtree(out / entry.name)
            (out / entry.name).symlink_to(outside)

        clean.execute_plan(entries, out, before_delete=swap)
        self.assertEqual(entries[0].outcome, "skipped")
        self.assertTrue((outside / "keep").exists())

    def test_moved_original_is_left_alone_when_the_name_is_reused(self):
        out = self.sandbox.root / "src" / "out"
        (out / "Debug_arm64").mkdir(parents=True)
        entries = clean.plan_cleanup(out, ["mac"], ["debug"], None, False)
        moved = self.sandbox.root / "moved"

        def swap(entry):
            os.rename(out / entry.name, moved)
            (out / entry.name).mkdir()
            (out / entry.name).joinpath("new").write_text("x")

        clean.execute_plan(entries, out, before_delete=swap)
        self.assertEqual(entries[0].outcome, "deleted")
        self.assertTrue(moved.exists())


if __name__ == "__main__":
    unittest.main()
