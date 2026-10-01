# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Operation records: dispatch persisted before mutation, every mutating command covered, actual final statuses."""

import json
import signal
import subprocess
import time
import unittest

from tests.integration.test_android import AndroidTestCase
from tests.integration.test_build import SKIP, BuildTestCase
from tests.support import SCRIPTS


class RecordCase:
    def records(self, command=None):
        directory = self.sandbox.config.parent / ".bdev" / "operations"
        found = [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]
        return [item for item in found if command is None or item["command"] == command]

    def only_record(self, command):
        (record,) = self.records(command)
        return record

    def record_of(self, document):
        (record,) = [item for item in self.records() if item["operation_id"] == document["operation_id"]]
        return record


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class RecordLifecycleTests(RecordCase, BuildTestCase):
    def test_json_build_logs_each_dependency_command_and_directory(self):
        from scaffold.common.procs import format_command_block
        repositories = [self.sandbox.add_dependency("main", relative) for relative in ("v8", "third_party/ffmpeg")]
        result, document = self.document("build")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"))
        for repository in repositories:
            for args in (["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
                         ["status", "--porcelain", "-z", "--untracked-files=no"]):
                argv = ["git", "--literal-pathspecs", "-C", str(repository), *args]
                self.assertIn(format_command_block(argv, str(repository)), result.stderr)
        self.assertNotIn("Command:", result.stdout)

    def test_a_failed_test_run_is_recorded_with_its_dispatch_child_status_and_id(self):
        result, document = self.document("test", "brave_unit_tests", env=self.env(FAKE_EXIT="2"))
        self.assertEqual(document["error"]["code"], "CHILD_FAILED")
        record = self.only_record("test")
        self.assertEqual(document["operation_id"], record["operation_id"])
        self.assertEqual((record["state"], record["status"], record["exit_code"], record["child_exit_code"]),
                         ("complete", "error", 5, 2))
        self.assertEqual(record["error"]["code"], "CHILD_FAILED")
        self.assertTrue(any(c["argv"][-2:-1] == ["brave_unit_tests"] or "brave_unit_tests" in c["argv"]
                            for c in record["commands"]), record["commands"])

    def test_dispatch_is_saved_before_the_child_finishes(self):
        process = subprocess.Popen(
            [str(SCRIPTS / "bdev"), "--json", "--config", self.config, "--checkout", "main", "build"],
            env=self.env(FAKE_SLEEP="60"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not [r for r in self.node_calls() if "build" in r["argv"]] and time.time() < deadline:
            time.sleep(0.05)
        record = self.only_record("build")
        process.kill()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(record["state"], "incomplete", "a killed process leaves an incomplete record")
        self.assertTrue([c for c in record["commands"] if "build" in c["argv"]], "the package command was recorded")

    def test_cancellation_records_the_signal_that_was_received(self):
        for signum, expected in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.subTest(signal=signum.name):
                self.sandbox.record.unlink(missing_ok=True)
                process = subprocess.Popen(
                    [str(SCRIPTS / "bdev"), "--json", "--config", self.config, "--checkout", "main", "test",
                     "brave_unit_tests"], env=self.env(FAKE_SLEEP="60"), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                deadline = time.time() + 30
                while not self.node_calls() and time.time() < deadline:
                    time.sleep(0.05)
                process.send_signal(signum)
                stdout, _ = process.communicate(timeout=60)
                document = json.loads(stdout)
                self.assertEqual((process.returncode, document["status"]), (expected, "cancelled"))
                record = self.record_of(document)
                self.assertEqual((record["state"], record["status"], record["exit_code"]),
                                 ("complete", "cancelled", expected))
                self.assertEqual(record["cleanup"], {"complete": True})
                self.assertEqual(document["operation_id"], record["operation_id"])

    def steps(self, record):
        return {step["name"]: step for step in record["steps"]}

    def test_a_launch_failure_after_a_build_keeps_the_completed_build_in_the_error_and_the_record(self):
        result, document = self.document("build-run", env=self.env(FAKE_OPEN_EXIT="1"))
        self.assertEqual(document["error"]["code"], "LAUNCH_FAILED")
        completed = {item["phase"]: item for item in document["error"]["details"]["completed_phases"]}
        self.assertEqual(completed["build"]["exit"], 0)
        self.assertEqual(completed["verify-output"]["artifact_status"], "verified")
        self.assertEqual([item["path"] for item in document["artifacts"]], [str(self.output_app())])
        record = self.record_of(document)
        steps = self.steps(record)
        self.assertEqual((steps["build"]["status"], steps["build"]["outcome"]["exit"]), ("succeeded", 0))
        self.assertEqual(steps["run"]["status"], "failed")
        self.assertEqual([item["path"] for item in record["artifacts"]], [str(self.output_app())])
        self.assertNotIn("planned", [step["status"] for step in record["steps"]], "no step is left as a description")

    def test_a_failed_build_after_a_sync_keeps_the_completed_sync(self):
        self.hook = self.sandbox.hook("if 'build' in argv:\n    raise SystemExit(3)\nraise SystemExit(0)\n")
        result, document = self.document("sync-build")
        self.assertEqual((document["error"]["code"], document["child_exit_code"]), ("CHILD_FAILED", 3))
        completed = {item["phase"]: item for item in document["error"]["details"]["completed_phases"]}
        self.assertIn("revisions_after", completed["sync"])
        self.assertNotIn("build", completed)
        steps = self.steps(self.record_of(document))
        self.assertEqual((steps["sync"]["status"], steps["build"]["status"]), ("succeeded", "failed"))
        self.assertEqual(steps["build"]["outcome"]["exit"], 3)

    def test_a_cancelled_phase_is_recorded_as_interrupted_and_a_killed_process_leaves_it_running(self):
        for how in ("cancel", "kill"):
            with self.subTest(how=how):
                self.sandbox.record.unlink(missing_ok=True)
                earlier = {item["operation_id"] for item in self.records("test")}
                process = subprocess.Popen(
                    [str(SCRIPTS / "bdev"), "--json", "--config", self.config, "--checkout", "main", "test",
                     "brave_unit_tests"], env=self.env(FAKE_SLEEP="60"), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                deadline = time.time() + 30
                while not self.node_calls() and time.time() < deadline:
                    time.sleep(0.05)
                if how == "cancel":
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=60)
                else:
                    process.kill()
                    process.wait(timeout=10)
                    # A surviving child can hold inherited pipes until fixture cleanup.
                    process.stdout.close()
                    process.stderr.close()
                (record,) = [item for item in self.records("test") if item["operation_id"] not in earlier]
                expected = "interrupted" if how == "cancel" else "running"
                self.assertEqual(self.steps(record)["test"]["status"], expected)
                self.assertEqual(record["state"], "complete" if how == "cancel" else "incomplete")

    def test_a_failed_restart_after_a_build_finishes_the_record_and_keeps_the_build_phase(self):
        result, document = self.document("build-run", env=self.env(FAKE_OPEN_EXIT="1"))
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "LAUNCH_FAILED"))
        record = self.only_record("build-run")
        self.assertEqual((record["state"], record["status"], record["exit_code"]), ("complete", "error", 5))
        names = [step["name"] for step in record["steps"]]
        self.assertIn("build", names)
        self.assertIn("run", names)
        self.assertEqual(document["operation_id"], record["operation_id"])

    def test_standalone_run_and_executed_cleanup_are_recorded_and_preview_is_not(self):
        self.assertEqual(self.document("build")[0].returncode, 0)
        result, document = self.document("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.only_record("run")
        self.assertEqual((record["state"], record["status"]), ("complete", "ok"))
        self.assertEqual(document["operation_id"], record["operation_id"])
        self.assertTrue(record["artifacts"])
        self.document("clean")
        self.assertEqual(self.records("clean"), [], "a preview changes nothing and leaves no record")
        result, document = self.document("clean", "--execute")
        record = self.only_record("clean")
        self.assertEqual((record["state"], record["status"]), ("complete", "ok"))
        self.assertEqual(record["details"]["deleted"], ["Debug_arm64"])
        self.assertEqual(document["operation_id"], record["operation_id"])

    def test_tool_setup_is_recorded(self):
        self.sandbox.mark_stale("main")
        result, document = self.document("tools", "setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.only_record("tools setup")
        self.assertEqual((record["state"], record["status"]), ("complete", "ok"))
        self.assertTrue(record["commands"])

    def test_records_carry_environment_source_and_log_evidence(self):
        result, document = self.document("build")
        record = self.only_record("build")
        environment = record["environment"]
        self.assertTrue(environment["file"].endswith("/.envrc"))
        self.assertRegex(environment["sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(environment["selects"]["BRAVE_CORE_DIR"], str(self.core))
        self.assertRegex(record["source"]["core_head"], "^[0-9a-f]{40}$")
        self.assertRegex(record["source"]["chromium_head"], "^[0-9a-f]{40}$")
        self.assertIsInstance(record["source"]["core_uncommitted_files"], int)
        self.assertEqual(record["logs"], {"commands": "stderr", "child_output": "stderr"})
        self.assertEqual((record["details"]["target"], record["details"]["configuration"],
                          record["details"]["arch"]), ("mac", "Debug", "arm64"))
        self.assertEqual(record["child_exit_code"], 0)
        self.assertTrue(record["commands"])
        self.sandbox.config.write_text(self.sandbox.config.read_text() + "\n[logging]\ncommands = false\n")
        _, document = self.document("build")
        record = self.record_of(document)
        self.assertEqual(record["logs"]["commands"], "disabled")
        self.assertTrue(record["commands"], "dispatch is still recorded")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class AndroidRecordTests(RecordCase, AndroidTestCase):
    def test_dependency_setup_and_device_runs_are_recorded(self):
        self.assertEqual(self.setup_support().returncode, 0)
        record = self.only_record("android setup")
        self.assertEqual((record["state"], record["status"]), ("complete", "ok"))
        self.assertTrue(record["commands"])
        self.sandbox.configure_rbe("main")
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['android']\n")
        self.assertEqual(self.document("build", "android")[0].returncode, 0)
        env = self.env(FAKE_ADB_DEVICES="emulator-5554,device")
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "deploy", "android",
                                   env=env)
        document = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.only_record("deploy")
        self.assertEqual((record["state"], record["status"]), ("complete", "ok"))
        self.assertEqual(document["operation_id"], record["operation_id"])


if __name__ == "__main__":
    unittest.main()
