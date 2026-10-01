# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android restart completion requires process confirmation."""

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import adb, records
from scaffold.common.procs import CommandLog
from scaffold.common.results import Cancelled, Result, ScaffoldError


def child(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["adb"], code, stdout, stderr)


class RestartPhaseTests(unittest.TestCase):
    def test_launch_is_not_completed_without_a_confirmed_pid(self):
        completed = []
        responses = [child(stdout="Success"), child(), child(), child(1)]
        with mock.patch.object(adb, "_adb", side_effect=responses):
            with self.assertRaises(ScaffoldError):
                adb.restart_package("adb", "device", "app.apk", "com.brave.app", {}, verify_seconds=0,
                                    progress=lambda name, **outcome: completed.append(name))
        self.assertEqual(completed, ["install-apk", "stop-package"])

    def recorded_restart(self, responses, cancel_after_install=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = SimpleNamespace(core=root / "src/brave", src=root / "src")
            ctx = SimpleNamespace(state_root=root, log=CommandLog(enabled=False))
            error = None
            with mock.patch.object(records, "describe_start", return_value={}), \
                    mock.patch.object(adb, "_adb", side_effect=responses) as commands:
                try:
                    with records.track(ctx, "run", identity, {}) as op:
                        op.start("run")
                        def progress(name, **outcome):
                            op.succeed(name, **outcome)
                            if cancel_after_install and name == "install-apk":
                                raise Cancelled(130)
                        adb.restart_package("adb", "device", "app.apk", "com.brave.app", {}, verify_seconds=0,
                                            progress=progress, started=op.start, failed=op.fail)
                        op.succeed("run")
                        op.complete(Result(command="run"))
                except (ScaffoldError, Cancelled) as caught:
                    error = caught
            phases = {step["name"]: step for step in op.data["steps"] if step["name"] != "run"}
            return phases, error, commands.call_count

    def test_stop_failure_keeps_install_success_and_leaves_launch_unattempted(self):
        phases, error, count = self.recorded_restart([child(stdout="Success"), child(8)])
        self.assertEqual(list(phases), ["install-apk", "stop-package"])
        self.assertEqual(phases["install-apk"]["status"], "succeeded")
        self.assertEqual(phases["install-apk"]["outcome"]["exit"], 0)
        self.assertEqual(phases["stop-package"]["status"], "failed")
        self.assertEqual(phases["stop-package"]["outcome"]["exit"], 8)
        self.assertEqual((error.child_exit_code, count), (8, 2))

    def test_launch_command_failure_records_its_exit_without_process_verification(self):
        phases, error, count = self.recorded_restart([child(stdout="Success"), child(), child(9)])
        self.assertEqual(phases["launch-package"]["status"], "failed")
        self.assertEqual(phases["launch-package"]["outcome"], {"exit": 9})
        self.assertEqual((error.child_exit_code, count), (9, 3))

    def test_missing_pid_marks_launch_failed_and_keeps_the_command_exit(self):
        phases, error, count = self.recorded_restart([child(stdout="Success"), child(), child(), child(1)])
        self.assertEqual(phases["launch-package"]["status"], "failed")
        self.assertEqual(phases["launch-package"]["outcome"], {"exit": 0, "verification_exit": 1})
        self.assertEqual(error.details["phase"], "launch-package")
        self.assertEqual([item["phase"] for item in error.details["completed_phases"]],
                         ["install-apk", "stop-package"])
        self.assertEqual((error.child_exit_code, count), (0, 4))

    def test_successful_restart_records_a_confirmed_pid(self):
        phases, error, count = self.recorded_restart([child(stdout="Success"), child(), child(), child(stdout="567")])
        self.assertIsNone(error)
        self.assertTrue(all(step["status"] == "succeeded" for step in phases.values()))
        self.assertEqual(phases["launch-package"]["outcome"], {"exit": 0, "verification_exit": 0, "pid": "567"})
        self.assertEqual(count, 4)

    def test_cancellation_between_phases_keeps_later_phases_unattempted(self):
        phases, error, count = self.recorded_restart([child(stdout="Success")], cancel_after_install=True)
        self.assertEqual(list(phases), ["install-apk"])
        self.assertEqual(phases["install-apk"]["status"], "succeeded")
        self.assertEqual(phases["install-apk"]["outcome"]["exit"], 0)
        self.assertEqual([item["phase"] for item in error.completed_phases], ["install-apk"])
        self.assertEqual((error.exit_code, count), (130, 1))
