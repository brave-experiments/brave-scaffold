# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Per-output build state survives damaged files without forgetting that the output is uncertain."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import records
from scaffold.brave.records import OutputState
from scaffold.common import config as config_module


class OutputStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.identity = SimpleNamespace(core=self.root / "checkout" / "src" / "brave")
        self.output = self.root / "checkout" / "src" / "out" / "Debug_arm64"

    def state(self):
        return OutputState(self.identity, self.output, self.root)

    def damaged(self, text):
        state = self.state()
        state.path.parent.mkdir(parents=True, exist_ok=True)
        state.path.write_text(text)
        return state.path

    def test_a_new_output_has_no_history_and_needs_no_revalidation(self):
        state = self.state()
        self.assertFalse(state.needs_revalidation)
        self.assertIsNone(state.success)

    def test_state_files_are_named_by_a_sha256_of_the_output_directory(self):
        expected = hashlib.sha256(os.path.realpath(self.output).encode()).hexdigest()[:16] + ".json"
        self.assertEqual(self.state().path.name, expected)

    def test_a_state_file_from_the_previous_naming_scheme_is_adopted_and_then_replaced(self):
        output = os.path.realpath(self.output)
        folder = self.state().path.parent
        folder.mkdir(parents=True)
        record = {"output_dir": output, "checkout": str(self.identity.core), "needs_revalidation": True,
                  "attempts": [], "history": [], "success": {"operation_id": "op-old", "artifact": {}, "fingerprint": {}}}
        previous = folder / (hashlib.sha1(output.encode(), usedforsecurity=False).hexdigest()[:16] + ".json")
        previous.write_text(json.dumps(record))
        elsewhere = folder / "unrelated.json"
        elsewhere.write_text(json.dumps(dict(record, output_dir=output + "-other")))
        state = self.state()
        self.assertEqual((state.success["operation_id"], state.needs_revalidation), ("op-old", True))
        self.assertTrue(previous.exists(), "reading changes nothing")
        state.begin_attempt("op-new")
        self.assertFalse(previous.exists(), "the old name is replaced once the new file is written")
        self.assertTrue(state.path.exists())
        self.assertTrue(elsewhere.exists(), "another output's record is never touched")
        again = self.state()
        self.assertEqual(again.success["operation_id"], "op-old")
        self.assertEqual(again.last_attempt()["operation_id"], "op-new")

    def test_a_damaged_file_means_the_output_needs_revalidation(self):
        for text in ('{"success": ', "", "[]", "null"):
            with self.subTest(text=text):
                path = self.damaged(text)
                state = self.state()
                self.assertTrue(state.needs_revalidation)
                self.assertIsNone(state.success)
                self.assertEqual(path.read_text(), text, "reading does not rewrite the evidence")
                path.unlink()

    def test_the_damaged_file_is_kept_aside_when_the_state_is_next_written(self):
        path = self.damaged('{"success": ')
        state = self.state()
        state.begin_attempt("op-1")
        kept = [item for item in path.parent.iterdir() if item != path]
        self.assertEqual([item.read_text() for item in kept], ['{"success": '])
        saved = json.loads(path.read_text())
        self.assertTrue(saved["needs_revalidation"])
        self.assertTrue(self.state().needs_revalidation, "the marker survives the rewrite")

    def test_only_a_validated_build_clears_the_marker_after_damage(self):
        self.damaged("{")
        state = self.state()
        state.begin_attempt("op-1")
        state.end_attempt_completed("op-1")
        self.assertTrue(state.needs_revalidation)
        state.record_success("op-2", {"path": "x"}, {})
        self.assertFalse(self.state().needs_revalidation)


class DamagedRecordListingTests(unittest.TestCase):
    def test_unreadable_records_are_listed_and_valid_ones_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = SimpleNamespace(core=root / "checkout" / "src" / "brave")
            good = OutputState(identity, root / "out" / "Good", root)
            good.begin_attempt("op-1")
            folder = good.path.parent
            for name, text in (("a.json", "{"), ("b.json", ""), ("c.json", "[]"), ("d.json", "null")):
                (folder / name).write_text(text)
            (folder / "e.json.damaged-20261007T000000").write_text("{")
            damaged = records.damaged_output_records(identity, root)
            self.assertEqual(sorted(Path(path).name for path in damaged), ["a.json", "b.json", "c.json", "d.json"])
            self.assertEqual(len(records.output_states(identity, root)), 1)

    def test_no_records_means_nothing_is_damaged(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = SimpleNamespace(core=Path(directory) / "checkout" / "src" / "brave")
            self.assertEqual(records.damaged_output_records(identity, Path(directory)), [])


class AtomicWriteTests(unittest.TestCase):
    def test_contents_reach_disk_before_the_file_is_swapped_in(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            order = []
            real_fsync, real_replace = os.fsync, os.replace
            with mock.patch.object(os, "fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))[1]), \
                    mock.patch.object(os, "replace", side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))[1]):
                config_module.atomic_write(target, "{}\n")
            self.assertEqual(order, ["fsync", "replace"])
            self.assertEqual(target.read_text(), "{}\n")

    def test_an_existing_file_keeps_its_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in (0o644, 0o640, 0o600):
                with self.subTest(mode=oct(mode)):
                    target = Path(directory) / ("file-%o" % mode)
                    target.write_text("old\n")
                    target.chmod(mode)
                    config_module.atomic_write(target, "new\n")
                    self.assertEqual((target.read_text(), target.stat().st_mode & 0o7777), ("new\n", mode))

    def test_a_symlinked_file_is_updated_in_place_and_stays_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "dotfiles" / "brave-scaffold.toml"
            real.parent.mkdir()
            real.write_text("old\n")
            real.chmod(0o644)
            link = Path(directory) / "brave-scaffold.toml"
            link.symlink_to(real)
            config_module.atomic_write(link, "new\n")
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(real))
            self.assertEqual((real.read_text(), real.stat().st_mode & 0o7777), ("new\n", 0o644))
            self.assertEqual([item.name for item in real.parent.iterdir()], ["brave-scaffold.toml"], "no temp files")

    def test_a_new_file_and_its_directories_are_created(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "a" / "b" / "state.json"
            config_module.atomic_write(target, "{}\n")
            self.assertEqual(target.read_text(), "{}\n")


if __name__ == "__main__":
    unittest.main()
