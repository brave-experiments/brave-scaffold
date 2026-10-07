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
from scaffold.brave import clean, records
from tests.schema_validation import Validator

NAMES = ["Debug_arm64", "Debug_x64", "Release_arm64", "DebugOrigin_arm64", "Debug", "android_Debug_arm64",
         "android_tests_Debug_arm64", "android_Release_arm64", "ios_Debug_arm64_simulator", "ios_Debug_x64_simulator",
         "ios_Debug_xcode_derived_data", "ios_Release_arm64_simulator", "Default", "Debugger"]


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
        return self.sandbox.bcore_json("clean", "--checkout", "main", "--config", self.config, *args)

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

    def test_output_explains_default_and_explicit_platform_scope(self):
        cases = (((), "host", ["mac"], "host default only"),
                 (("android", "--configuration", "debug", "--arch", "arm64"), "explicit", ["android"],
                  "explicit selection"),
                 (("macos",), "explicit", ["mac"], "explicit selection"),
                 (("all",), "explicit", ["mac", "android", "ios"], "explicit selection"))
        for args, source, targets, label in cases:
            with self.subTest(args=args):
                result = self.sandbox.bcore("clean", "--checkout", "main", "--config", self.config, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                scope = "Platforms: %s (%s)" % (", ".join(targets), label)
                self.assertIn(scope, result.stdout)
                self.assertLess(result.stdout.index(scope), result.stdout.index("planned"))
                configurations, arch = ("debug", "arm64") if source == "explicit" and targets == ["android"] \
                    else ("debug, release", "all")
                self.assertIn("Configurations: %s; architecture: %s" % (configurations, arch), result.stdout)
                self.assertEqual("To preview every platform" in result.stdout, source == "host")
                _, document = self.clean(*args)
                self.assertEqual(document["data"]["targets"], targets)
                self.assertEqual(document["data"]["target_source"], source)
                self.assertEqual(Validator().problems(document), [])

    def test_configured_default_scope_is_visible_even_when_no_output_matches(self):
        self.sandbox.write_config([("main", self.core, None)], extra='\n[defaults]\nplatform = "android"\n')
        for name in NAMES:
            if name.startswith("android_"):
                shutil.rmtree(self.out / name)
        result = self.sandbox.bcore("clean", "--config", self.config, "--checkout", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Platforms: android (configured default only)", result.stdout)
        self.assertIn("Other platforms are not checked", result.stdout)
        self.assertIn("bcore clean all", result.stdout)
        self.assertIn("No matching build output directories", result.stdout)
        _, document = self.clean()
        self.assertEqual(document["data"]["target_source"], "configured")
        self.assertEqual(document["data"]["targets"], ["android"])

    def test_arch_narrows_the_match(self):
        self.clean("mac", "--arch", "arm64", "--execute")
        self.assertIn("Debug_x64", self.remaining())
        self.assertNotIn("Debug_arm64", self.remaining())
        self.assertNotIn("DebugOrigin_arm64", self.remaining())

    def make_dirs(self, *names):
        for name in names:
            (self.out / name / "obj").mkdir(parents=True)
            (self.out / name / "obj" / "x.o").write_text(name)

    def test_an_unsuffixed_directory_is_the_x64_output_and_survives_an_arm64_clean(self):
        self.make_dirs("Release", "DebugOrigin", "android_Debug", "ios_Debug_simulator")
        result, document = self.clean("mac", "--arch", "arm64")
        self.assertEqual(self.names(document), ["DebugOrigin_arm64", "Debug_arm64", "Release_arm64"])
        self.clean("all", "--arch", "arm64", "--execute")
        for survivor in ("Debug", "Release", "DebugOrigin", "Debug_x64", "android_Debug", "ios_Debug_simulator",
                         "ios_Debug_x64_simulator"):
            self.assertIn(survivor, self.remaining(), survivor)
        for gone in ("Debug_arm64", "Release_arm64", "DebugOrigin_arm64", "android_Debug_arm64",
                     "ios_Debug_arm64_simulator"):
            self.assertNotIn(gone, self.remaining(), gone)

    def test_x64_means_the_unsuffixed_directory_and_an_explicit_x64_suffix(self):
        self.make_dirs("Release", "DebugOrigin", "android_Debug", "ios_Debug_simulator")
        result, document = self.clean("mac", "--arch", "x64")
        self.assertEqual(self.names(document), ["Debug", "DebugOrigin", "Debug_x64", "Release"])
        result, document = self.clean("android", "--arch", "x64")
        self.assertEqual(self.names(document), ["android_Debug"])
        result, document = self.clean("ios", "--arch", "x64")
        self.assertEqual(self.names(document), ["ios_Debug_simulator", "ios_Debug_x64_simulator"],
                         "derived data holds products for every architecture and is only matched without --arch")

    def test_without_an_arch_every_architecture_matches(self):
        self.make_dirs("Release", "android_Debug")
        result, document = self.clean("mac")
        self.assertEqual(self.names(document), ["Debug", "DebugOrigin_arm64", "Debug_arm64", "Debug_x64", "Release",
                                                "Release_arm64"])

    def test_all_is_explicit_and_omission_is_not_all(self):
        self.clean("--execute")
        self.assertIn("android_Debug_arm64", self.remaining())
        self.clean("all", "--execute")
        self.assertEqual(self.remaining(), ["Debugger", "Default"])

    def test_unknown_and_unavailable_targets_are_rejected_without_deleting(self):
        result, document = self.clean("nonsense", "--execute")
        self.assertEqual(document["error"]["code"], "INVALID_INPUT")
        self.assertEqual(self.remaining(), sorted(NAMES))

    def test_ios_matches_simulator_outputs_and_derived_data_for_the_configuration(self):
        result, document = self.clean("ios", "--configuration", "debug")
        self.assertEqual(self.names(document), ["ios_Debug_arm64_simulator", "ios_Debug_x64_simulator",
                                                "ios_Debug_xcode_derived_data"])
        result, document = self.clean("ios", "--configuration", "debug", "--arch", "arm64", "--execute")
        self.assertEqual(self.names(document), ["ios_Debug_arm64_simulator"])
        self.assertNotIn("ios_Debug_arm64_simulator", self.remaining())
        self.assertIn("ios_Debug_x64_simulator", self.remaining())

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

    def plan(self):
        out = self.sandbox.root / "src" / "out"
        (out / "Debug_arm64").mkdir(parents=True)
        (out / "Debug_arm64" / "original").write_text("approved")
        return out, clean.plan_cleanup(out, ["mac"], ["debug"], None, False)

    def substitute(self, out, name):
        """Move the approved directory away and put a different ordinary directory at the same path."""
        moved = self.sandbox.root / "moved"
        os.rename(out / name, moved)
        (out / name).mkdir()
        (out / name / "replacement").write_text("not approved")
        return moved

    def test_a_different_directory_at_the_planned_path_is_never_deleted(self):
        out, entries = self.plan()
        moved = []
        clean.execute_plan(entries, out, before_delete=lambda entry: moved.append(self.substitute(out, entry.name)))
        self.assertEqual((entries[0].outcome, entries[0].detail), ("skipped", "changed after the plan was made"))
        self.assertEqual((out / "Debug_arm64" / "replacement").read_text(), "not approved")
        self.assertEqual((moved[0] / "original").read_text(), "approved")

    def test_substitution_during_execution_is_detected_and_the_replacement_is_kept(self):
        out, entries = self.plan()
        moved = []
        clean.execute_plan(entries, out, during_delete=lambda entry: moved.append(self.substitute(out, entry.name)))
        self.assertEqual(entries[0].outcome, "skipped")
        self.assertEqual((out / "Debug_arm64" / "replacement").read_text(), "not approved")
        self.assertEqual((moved[0] / "original").read_text(), "approved")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["Debug_arm64"], "nothing is left half-renamed")

    def test_a_git_entry_added_immediately_before_deletion_is_preserved(self):
        out, entries = self.plan()
        def add_repository(entry):
            (out / entry.name / ".git").mkdir()
            (out / entry.name / ".git/HEAD").write_text("wanted HEAD")
        clean.execute_plan(entries, out, during_delete=add_repository)
        self.assertEqual(entries[0].outcome, "skipped")
        self.assertIn(".git", entries[0].detail)
        remaining = out / (entries[0].private or entries[0].name)
        self.assertEqual((remaining / ".git/HEAD").read_text(), "wanted HEAD")
        self.assertEqual((remaining / "original").read_text(), "approved")

    def test_an_out_directory_replaced_after_planning_is_refused(self):
        out, entries = self.plan()
        elsewhere = self.sandbox.root / "old-out"

        def swap_out(entry):
            os.rename(out, elsewhere)
            (out / "Debug_arm64").mkdir(parents=True)
            (out / "Debug_arm64" / "replacement").write_text("not approved")

        clean.execute_plan(entries, out, before_delete=swap_out)
        self.assertEqual(entries[0].outcome, "skipped")
        self.assertTrue((out / "Debug_arm64" / "replacement").exists())
        self.assertTrue((elsewhere / "Debug_arm64" / "original").exists())

    def test_a_deletion_that_cannot_finish_says_where_the_remainder_is(self):
        out, entries = self.plan()
        locked = out / "Debug_arm64" / "locked"
        locked.mkdir()
        (locked / "file").write_text("x")
        locked.chmod(0o500)
        self.addCleanup(lambda: [p.chmod(0o700) for p in out.rglob("locked")])
        clean.execute_plan(entries, out)
        self.assertEqual(entries[0].outcome, "failed")
        remainder = [p.name for p in out.iterdir()]
        self.assertEqual(len(remainder), 1)
        self.assertTrue(remainder[0].startswith(".scaffold-deleting-Debug_arm64-"))
        self.assertIn(remainder[0], entries[0].detail)

    def test_links_inside_an_approved_directory_are_removed_not_followed(self):
        out, entries = self.plan()
        outside = self.sandbox.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x")
        (out / "Debug_arm64" / "link").symlink_to(outside)
        (out / "Debug_arm64" / "nested" / "deeper").mkdir(parents=True)
        (out / "Debug_arm64" / "nested" / "deeper" / "file").write_text("x")
        clean.execute_plan(entries, out)
        self.assertEqual(entries[0].outcome, "deleted")
        self.assertEqual(list(out.iterdir()), [])
        self.assertTrue((outside / "keep").exists())

class InterruptedCleanupTests(SandboxTest):
    """A deletion that is cancelled after the private rename leaves a remainder that later runs can find."""

    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout("main")
        self.out = self.core.parent / "out"
        (self.out / "Debug_arm64" / "obj").mkdir(parents=True)
        (self.out / "Debug_arm64" / "obj" / "x.o").write_text("approved")
        (self.out / "Debug_arm64" / "args.gn").write_text("approved")
        (self.out / "Release_arm64").mkdir()
        self.sandbox.write_config([("main", self.core, None)])
        self.config = str(self.sandbox.config)

    def run_cli(self, *args, patched=None):
        import contextlib
        import io
        import json
        from unittest import mock
        from scaffold.brave import app, bcore
        spec, tokens = bcore.resolve_command(["--json", "clean", "--checkout", "main", "--config", self.config, *args])
        parsed = bcore.parse_command(spec, tokens)
        stdout = io.StringIO()
        with contextlib.ExitStack() as stack:
            if patched:
                stack.enter_context(mock.patch.object(clean, "_remove_contents", patched))
            code = app.run_command(spec.name, parsed, spec.handler, argv_environ=self.sandbox.env(),
                                   stdout=stdout, stderr=io.StringIO())
        return code, json.loads(stdout.getvalue())

    def record(self):
        import json
        (path,) = (self.sandbox.config.parent / ".bcore" / "operations").glob("*.json")
        return json.loads(path.read_text())

    def interrupt_after_rename(self, partial):
        from scaffold.common.results import Cancelled

        def interrupted(directory_fd):
            if partial:
                os.unlink("args.gn", dir_fd=directory_fd)
            raise Cancelled(130)
        code, document = self.run_cli("mac", "--configuration", "debug", "--execute", patched=interrupted)
        self.assertEqual((code, document["status"]), (130, "cancelled"))
        return document

    def test_cleanup_never_sends_a_real_notification(self):
        from unittest import mock
        from scaffold.common import notify
        with mock.patch.object(notify.MacNotifier, "send") as send:
            code, _ = self.run_cli("mac", "--configuration", "debug", "--execute")
        self.assertEqual(code, 0)
        send.assert_not_called()

    def test_cancellation_after_the_rename_records_the_remainder_and_the_partial_outcome(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                shutil.rmtree(self.out)
                (self.out / "Debug_arm64" / "obj").mkdir(parents=True)
                (self.out / "Debug_arm64" / "obj" / "x.o").write_text("approved")
                (self.out / "Debug_arm64" / "args.gn").write_text("approved")
                shutil.rmtree(self.sandbox.config.parent / ".bcore", ignore_errors=True)
                self.interrupt_after_rename(partial)
                (remainder,) = [p for p in self.out.iterdir() if p.name.startswith(".scaffold-deleting-Debug_arm64-")]
                record = self.record()
                (step,) = [item for item in record["steps"] if item["name"] == "delete"]
                self.assertEqual((step["status"], step["private"], step["directory"]),
                                 ("interrupted", remainder.name, "Debug_arm64"))
                self.assertEqual(step["identity"], [os.lstat(remainder).st_dev, os.lstat(remainder).st_ino])
                self.assertEqual(record["details"]["interrupted"], ["Debug_arm64"])
                self.assertEqual(record["details"]["remaining"], [remainder.name])

    def test_the_next_cleanup_finds_the_owned_remainder_and_leaves_lookalikes_alone(self):
        self.interrupt_after_rename(partial=True)
        (remainder,) = [p for p in self.out.iterdir() if p.name.startswith(".scaffold-deleting-Debug_arm64-")]
        lookalike = self.out / ".scaffold-deleting-Debug_arm64-0000"
        lookalike.mkdir()
        (lookalike / "keep").write_text("not ours")
        code, document = self.run_cli("mac", "--configuration", "debug")
        outcomes = {e["name"]: (e["outcome"], e["detail"]) for e in document["data"]["entries"]}
        self.assertEqual(outcomes[remainder.name][0], "planned")
        self.assertIn("unfinished deletion of Debug_arm64", outcomes[remainder.name][1])
        self.assertEqual(outcomes[lookalike.name][0], "skipped")
        self.assertIn("left alone", outcomes[lookalike.name][1])
        self.assertTrue(remainder.exists(), "the preview deletes nothing")
        code, document = self.run_cli("mac", "--configuration", "debug", "--execute")
        self.assertEqual(code, 6, "the lookalike is reported as skipped, so the run is partial")
        self.assertFalse(remainder.exists())
        self.assertEqual((lookalike / "keep").read_text(), "not ours")
        self.assertTrue((self.out / "Release_arm64").exists())

    def test_an_unfinished_deletion_of_an_unselected_target_does_not_make_the_run_partial(self):
        self.interrupt_after_rename(partial=True)
        (remainder,) = [p for p in self.out.iterdir() if p.name.startswith(".scaffold-deleting-Debug_arm64-")]
        code, document = self.run_cli("android", "--execute")
        self.assertEqual((code, document["status"]), (0, "ok"))
        self.assertEqual(document["warnings"], [])
        (entry,) = [e for e in document["data"]["entries"] if e["name"] == remainder.name]
        self.assertEqual(entry["outcome"], "unselected")
        self.assertIn("select its target to finish it", entry["detail"])
        self.assertTrue(remainder.exists(), "it is reported and left alone")
        code, document = self.run_cli("mac", "--configuration", "debug", "--execute")
        self.assertEqual((code, document["status"]), (0, "ok"))
        self.assertFalse(remainder.exists(), "selecting its target finishes it")

    def test_a_remainder_that_is_not_the_recorded_directory_is_never_deleted(self):
        self.interrupt_after_rename(partial=False)
        (remainder,) = [p for p in self.out.iterdir() if p.name.startswith(".scaffold-deleting-Debug_arm64-")]
        shutil.rmtree(remainder)
        remainder.mkdir()
        (remainder / "keep").write_text("a different directory under the same name")
        code, document = self.run_cli("mac", "--configuration", "debug", "--execute")
        self.assertEqual((remainder / "keep").read_text(), "a different directory under the same name")
        entry = next(e for e in document["data"]["entries"] if e["name"] == remainder.name)
        self.assertEqual(entry["outcome"], "skipped")

    def assert_resumed_repository_is_preserved(self, git_file):
        self.interrupt_after_rename(partial=False)
        (remainder,) = [p for p in self.out.iterdir() if p.name.startswith(clean.PRIVATE_PREFIX)]
        if git_file:
            (remainder / ".git").write_text("gitdir: recovered repository\n")
        else:
            (remainder / ".git").mkdir()
            (remainder / ".git/HEAD").write_text("wanted HEAD\n")
        (remainder / "source.cc").write_text("wanted recovered source\n")
        before = tree_snapshot(remainder)
        code, document = self.run_cli("mac", "--configuration", "debug", "--execute")
        self.assertEqual(code, 6)
        entry = next(e for e in document["data"]["entries"] if e["name"] == remainder.name)
        self.assertEqual(entry["outcome"], "skipped")
        self.assertIn(".git", entry["detail"])
        self.assertEqual(tree_snapshot(remainder), before)

    def test_resumed_cleanup_preserves_a_new_git_directory(self):
        self.assert_resumed_repository_is_preserved(git_file=False)

    def test_resumed_cleanup_preserves_a_new_git_file(self):
        self.assert_resumed_repository_is_preserved(git_file=True)

    def prune_after_later_operations(self):
        directory = self.sandbox.config.parent / ".bcore/operations"
        (original,) = directory.glob("*.json")
        oldest = directory / "000000-old-cleanup.json"
        original.rename(oldest)
        for _ in range(3):
            self.run_cli("mac", "--configuration", "release", "--execute")
        records.prune(directory.parent, keep=1)
        return oldest

    def assert_recovery_survives_pruning(self, failed):
        if failed:
            def failure(_):
                raise OSError("cannot delete")
            code, _ = self.run_cli("mac", "--configuration", "debug", "--execute", patched=failure)
            self.assertEqual(code, 6)
        else:
            self.interrupt_after_rename(partial=True)
        self.assertEqual(self.record()["state"], "complete")
        (remainder,) = [p for p in self.out.iterdir() if p.name.startswith(clean.PRIVATE_PREFIX)]
        oldest = self.prune_after_later_operations()
        self.assertTrue(oldest.exists(), "ownership must outlive bounded operation history")
        code, document = self.run_cli("mac", "--configuration", "debug")
        entry = next(e for e in document["data"]["entries"] if e["name"] == remainder.name)
        self.assertEqual(entry["outcome"], "planned")
        self.assertTrue(remainder.exists())
        code, document = self.run_cli("mac", "--configuration", "debug", "--execute")
        self.assertEqual(code, 0)
        self.assertFalse(remainder.exists())
        records.prune(oldest.parent.parent, keep=1)
        self.assertFalse(oldest.exists(), "removed remainder no longer needs ownership history")

    def test_cancelled_cleanup_ownership_survives_later_log_pruning(self):
        self.assert_recovery_survives_pruning(failed=False)

    def test_failed_cleanup_ownership_survives_later_log_pruning(self):
        self.assert_recovery_survives_pruning(failed=True)

    def test_stale_cleanup_ownership_can_be_pruned_when_the_remainder_is_gone(self):
        self.interrupt_after_rename(partial=False)
        (remainder,) = [p for p in self.out.iterdir() if p.name.startswith(clean.PRIVATE_PREFIX)]
        shutil.rmtree(remainder)
        oldest = self.prune_after_later_operations()
        self.assertFalse(oldest.exists())

    def test_a_cleanup_with_no_remainder_and_no_lookalike_is_unchanged(self):
        code, document = self.run_cli("mac", "--configuration", "debug", "--execute")
        self.assertEqual(code, 0)
        self.assertEqual([e["name"] for e in document["data"]["entries"]], ["Debug_arm64"])


if __name__ == "__main__":
    unittest.main()
