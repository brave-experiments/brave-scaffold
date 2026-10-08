# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""A test run is recorded as complete only after its results are verified, against the real output record."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import android_tests, cmd_build
from scaffold.brave.records import OutputState
from scaffold.common.results import ScaffoldError

PASSING = {"Foo.A": [{"status": "SUCCESS"}], "Foo.B": [{"status": "SUCCESS"}]}
FAILING = {"Foo.A": [{"status": "SUCCESS"}], "Foo.B": [{"status": "FAILURE"}]}


class AttemptFixture(unittest.TestCase):
    """A clean, successfully built output and the doubles every attempt test needs; defines no tests."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.identity = SimpleNamespace(core=self.root / "checkout" / "src" / "brave",
                                        src=self.root / "checkout" / "src")
        self.output = self.root / "checkout" / "src" / "out" / "Debug_arm64"
        self.output.mkdir(parents=True)
        self.results = self.output / "scaffold_test_results.json"
        clean = self.record()
        clean.record_success("op-build", {"path": "app"}, {})  # a good build, nothing uncertain about it
        self.ctx = mock.MagicMock()
        self.ctx.state_root = self.root
        self.execution = SimpleNamespace(identity=self.identity, environ={}, checks=[], toolchain=None,
                                         context=lambda ctx: ctx)
        self.effective = SimpleNamespace(target="mac", configuration="Debug", arch="arm64", output_dir=self.output,
                                         preparation_dir=self.output, offline=True)
        self.op = SimpleNamespace(id="op-test", detail=lambda **kwargs: None, start=lambda *a, **k: None,
                                  succeed=lambda *a, **k: None)

    def record(self):
        return OutputState(self.identity, self.output, self.root)

    def runner_writes(self, tests):
        """Stand in for the child process only: the attempt record is the real one, begun as run_output_step does."""
        def run_output_step(ctx, execution, effective, op, arguments, phase, extra_env=None, before_child=None):
            state = self.record()
            state.begin_attempt(op.id, True)
            if before_child:
                before_child()
            if tests is not None:
                self.results.write_text(json.dumps({"per_iteration_data": [tests]}))
            return ["pnpm", "run", "test"], state
        return run_output_step

    def run_desktop(self, tests):
        with mock.patch.object(cmd_build, "run_output_step", self.runner_writes(tests)), \
                mock.patch.object(cmd_build, "prepare_patches"), mock.patch.object(cmd_build, "log_test_phase"), \
                mock.patch.object(cmd_build, "metal_environment", return_value={}):
            return cmd_build.run_test_package(self.ctx, self.execution, self.effective, self.op, ["run", "test"],
                                              self.results)

    def assert_marked_failed(self):
        state = self.record()
        self.assertEqual(state.last_attempt()["outcome"], "failed")
        self.assertTrue(state.needs_revalidation, "a run that did not pass leaves the output marked")
        self.assertEqual(state.success["operation_id"], "op-build", "the earlier success stays as history")


class TestAttemptTests(AttemptFixture):
    def test_failed_tests_with_a_zero_exit_leave_the_attempt_failed_and_the_output_marked(self):
        with self.assertRaises(ScaffoldError) as caught:
            self.run_desktop(FAILING)
        self.assertEqual(caught.exception.code, "TEST_FAILED")
        self.assert_marked_failed()

    def test_a_run_that_ran_no_tests_leaves_the_attempt_failed_and_the_output_marked(self):
        with self.assertRaises(ScaffoldError) as caught:
            self.run_desktop({})
        self.assertEqual(caught.exception.code, "NO_TESTS_RAN")
        self.assert_marked_failed()

    def test_a_passing_run_is_complete_and_leaves_the_output_unmarked(self):
        argv, summary, warning = self.run_desktop(PASSING)
        self.assertEqual((summary["passed"], summary["failed"], warning), (2, 0, None))
        state = self.record()
        self.assertEqual(state.last_attempt()["outcome"], "succeeded")
        self.assertFalse(state.needs_revalidation)

    def test_an_unreadable_summary_is_unverified_not_failed(self):
        self.results.parent.mkdir(exist_ok=True)

        def garbled(ctx, execution, effective, op, arguments, phase, extra_env=None, before_child=None):
            state = self.record()
            state.begin_attempt(op.id, True)
            self.results.write_text("not json")
            return ["pnpm"], state
        with mock.patch.object(cmd_build, "run_output_step", garbled), mock.patch.object(cmd_build, "prepare_patches"), \
                mock.patch.object(cmd_build, "log_test_phase"), mock.patch.object(cmd_build, "metal_environment", return_value={}):
            argv, summary, warning = cmd_build.run_test_package(self.ctx, self.execution, self.effective, self.op,
                                                                ["run", "test"], self.results)
        self.assertIsNone(summary)
        self.assertIn("unverified", warning)
        self.assertEqual(self.record().last_attempt()["outcome"], "succeeded")

    def run_android(self, tests, verify_error=None):
        effective = SimpleNamespace(**{**vars(self.effective), "target": "android", "chosen_gn_keys": frozenset(),
                                       "generated": [], "forwarded": []})
        step = SimpleNamespace(name="gn-overrides", record=lambda: {})
        verify = mock.Mock(side_effect=verify_error) if verify_error else mock.Mock(return_value=({"ran": 2}, None))
        with mock.patch.object(cmd_build, "run_output_step", self.runner_writes(tests)), \
                mock.patch.object(cmd_build.step_module, "gn_step", return_value=step), \
                mock.patch.object(cmd_build.android, "write_gn_overrides"), \
                mock.patch.object(cmd_build.android, "build_environment", return_value={}), \
                mock.patch.object(android_tests, "verify_outcome", verify):
            return cmd_build.run_android_test_with_overlay(self.ctx, self.execution, effective, self.op, ["suite"], [],
                                                           "brave_junit_tests", self.results, None, None, False)

    def test_android_tests_are_recorded_the_same_way(self):
        failure = ScaffoldError("TEST_FAILED", "1 test(s) failed although the runner exited 0.", child_exit_code=0)
        with self.assertRaises(ScaffoldError):
            self.run_android(FAILING, verify_error=failure)
        self.assert_marked_failed()

    def test_a_verified_android_run_completes_the_attempt(self):
        argv, summary, warning = self.run_android(PASSING)
        self.assertEqual(summary, {"ran": 2})
        self.assertEqual(self.record().last_attempt()["outcome"], "succeeded")
        self.assertFalse(self.record().needs_revalidation)


class ConcludeDeviceAttemptTests(AttemptFixture):
    def begin(self):
        state = self.record()
        state.begin_attempt("op-devices", True)
        return state

    def conclude(self, passed):
        cmd_build.conclude_device_attempt(self.ctx, self.identity, self.effective, SimpleNamespace(id="op-devices"),
                                          passed)
        return self.record()

    def test_a_passing_aggregate_completes_the_attempt(self):
        self.begin()
        state = self.conclude(True)
        self.assertEqual(state.attempt_outcome("op-devices"), "succeeded")
        self.assertFalse(state.needs_revalidation)

    def test_a_failed_or_unverified_aggregate_leaves_the_attempt_failed_and_the_output_marked(self):
        self.begin()
        state = self.conclude(False)
        self.assertEqual(state.attempt_outcome("op-devices"), "failed")
        self.assertTrue(state.needs_revalidation)

    def test_an_attempt_the_child_failure_already_closed_is_never_overwritten(self):
        state = self.begin()
        state.end_attempt("op-devices", "failed")
        for passed in (True, False):
            self.assertEqual(self.conclude(passed).attempt_outcome("op-devices"), "failed")
            self.assertTrue(self.record().needs_revalidation)

    def test_an_unknown_attempt_is_left_alone(self):
        self.assertIsNone(self.conclude(True).attempt_outcome("op-devices"))


if __name__ == "__main__":
    unittest.main()
