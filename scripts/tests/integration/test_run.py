# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Run/restart behavior and failed or interrupted rebuilds."""

import json
import os
import signal
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.integration.test_build import BUILD_HOOK, SKIP, BuildTestCase
from tests.support import SCRIPTS, write_executable


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() \
        not in ("", "Z")


def wait_gone(pid, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class RunTests(BuildTestCase):
    def build(self, *args, **kwargs):
        result, document = self.document("build", *args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return document

    def run_app(self, *args, env=None):
        return self.document("run", *args, env=env)

    def launched(self):
        return [r for r in self.sandbox.records() if r["tool"] == "open"]

    def test_run_never_builds_and_teaches_the_missing_output_case(self):
        result, document = self.run_app()
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "ARTIFACT_MISSING"))
        commands = [step["argv"][:2] for step in document["error"]["repairs"]]
        self.assertIn(["bcore", "build"], commands)
        self.assertEqual(self.node_calls(), [])

    def test_run_with_nothing_running_launches_the_selected_bundle(self):
        self.build()
        result, document = self.run_app()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["path"] for r in self.launched()], [str(self.output_app())])
        run = document["data"]["run"]
        self.assertEqual(run["stopped"], [])
        self.assertTrue(run["launched_pid"])
        self.assertEqual(len([r for r in self.node_calls() if "build" in r["argv"]]), 1, "run must not build")

    def test_run_restarts_matching_instances_from_any_checkout_and_spares_others(self):
        self.build()
        elsewhere, matching = self.sandbox.start_app(self.sandbox.root / "other-checkout" / "out")
        unrelated, other = self.sandbox.start_app(self.sandbox.root / "unrelated", bundle_id="org.example.Other",
                                                  name="Example")
        result, document = self.run_app()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(wait_gone(matching.pid), "the same application from another checkout is restarted")
        self.assertTrue(alive(other.pid), "other applications are left alone")
        self.assertEqual(document["data"]["run"]["stopped"], [matching.pid])
        self.assertEqual([r["path"] for r in self.launched()], [str(self.output_app())])

    def test_a_failed_process_listing_stops_a_restart_before_anything_is_stopped_or_launched(self):
        self.build()
        write_executable(self.sandbox.bin / "ps", "#!/bin/sh\nexit 1\n")
        result, document = self.run_app()
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "LAUNCH_FAILED"))
        self.assertEqual([r for r in self.sandbox.records() if r["tool"] in ("open", "osascript")], [])

    def test_failed_preflight_leaves_the_running_browser_alone(self):
        _, running = self.sandbox.start_app(self.sandbox.root / "somewhere")
        result, document = self.run_app()
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISSING")
        self.assertTrue(alive(running.pid))
        result, document = self.run_app("--artifact", str(self.sandbox.root / "nope.app"))
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISSING")
        self.assertTrue(alive(running.pid))

    def test_wrong_product_is_rejected_before_stopping_anything(self):
        app, _ = self.sandbox.start_app(self.sandbox.root / "third-party", bundle_id="org.example.Other",
                                        name="Example")
        _, running = self.sandbox.start_app(self.sandbox.root / "somewhere")
        result, document = self.run_app("--artifact", app)
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISMATCH")
        self.assertTrue(alive(running.pid))

    def test_launch_failure_is_reported(self):
        self.build()
        result, document = self.run_app(env=self.env(FAKE_OPEN_EXIT="1"))
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "LAUNCH_FAILED"))

    def test_multiple_valid_outputs_require_an_explicit_artifact(self):
        self.build()
        self.build("-C", "Custom")
        result, document = self.run_app()
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "ARTIFACT_AMBIGUOUS"))
        self.assertEqual(len(document["error"]["details"]["candidates"]), 2)
        result, document = self.run_app("--artifact", str(self.src / "out" / "Custom" / "Brave Browser Development.app"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.launched()[-1]["path"], str(self.src / "out" / "Custom" / "Brave Browser Development.app"))

    def test_build_run_launches_exactly_the_artifact_it_built(self):
        self.build("-C", "Older")
        result, document = self.document("build-run", "-C", "Newer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["path"] for r in self.launched()], [str(self.src / "out" / "Newer" /
                                                                  "Brave Browser Development.app")])

    def test_run_does_not_inspect_sources_or_report_freshness(self):
        self.build()
        dependency = self.sandbox.add_dependency("main")
        (dependency / ".git/HEAD").write_text("invalid head\n")
        (self.src.parent / ".gclient_entries").write_text("invalid entries\n")
        (self.src / "base/BUILD.gn").write_text("edited after build\n")
        result, document = self.run_app()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("freshness", document["data"]["run"])
        self.assertFalse(any(w["code"] in ("STALE_BUILD", "UNKNOWN_FRESHNESS") for w in document["warnings"]))
        log = Path(next(line.removeprefix("Log: ") for line in result.stderr.splitlines()
                        if line.startswith("Log: "))).read_text()
        self.assertNotIn("Checking source state", log)
        self.assertNotIn(str(dependency), log)

    # --- failed and interrupted rebuilds ----------------------------------------

    def state(self, output="Debug_arm64"):
        from scaffold.brave.records import OutputState
        from scaffold.common.identity import build_identity
        from scaffold.common.config import load_config
        config = load_config(self.sandbox.config)
        identity = build_identity(self.core, config, "test")
        return OutputState(identity, self.src / "out" / output, self.sandbox.config.parent)

    def test_failed_rebuild_invalidates_the_record_but_older_output_still_runs(self):
        self.build()
        self.build("-C", "Separate")
        result, document = self.document("build", env=self.env(FAKE_EXIT="1"))
        self.assertEqual(document["error"]["code"], "CHILD_FAILED")
        self.assertTrue(self.state().needs_revalidation)
        self.assertFalse(self.state("Separate").needs_revalidation, "an untouched output is not invalidated")
        result, document = self.run_app("--artifact", str(self.output_app()))
        self.assertEqual(result.returncode, 0, "valid older output stays launchable")
        self.assertNotIn("freshness", document["data"]["run"])
        result, document = self.run_app("--artifact", str(self.src / "out" / "Separate" / "Brave Browser Development.app"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.build()
        self.assertFalse(self.state().needs_revalidation, "a later validated build restores the record")
        result, document = self.run_app("--artifact", str(self.output_app()))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_warns_when_the_output_needs_revalidation_without_inspecting_sources(self):
        self.build()
        result, document = self.run_app()
        self.assertEqual(document["warnings"], [])
        self.document("build", env=self.env(FAKE_EXIT="1"))
        result, document = self.run_app("--artifact", str(self.output_app()))
        self.assertEqual(result.returncode, 0, result.stderr)
        warning = next(w for w in document["warnings"] if w["code"] == "ARTIFACT_FRESHNESS_UNKNOWN")
        self.assertIn("partly overwritten", warning["message"])
        self.assertIn(str(self.output_app()), warning["message"])
        self.assertNotIn("freshness", document["data"]["run"])
        self.assertIn("Warning [ARTIFACT_FRESHNESS_UNKNOWN]", result.stderr)
        self.build()
        result, document = self.run_app()
        self.assertEqual(document["warnings"], [])

    def test_a_passing_test_run_does_not_leave_the_output_marked_uncertain(self):
        self.build()
        result, document = self.document("test", "brave_unit_tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state().needs_revalidation)
        self.assertEqual(self.state().last_attempt()["outcome"], "succeeded")
        result, document = self.run_app()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.document("test", "brave_unit_tests", env=self.env(FAKE_EXIT="1"))
        self.assertTrue(self.state().needs_revalidation, "a failed test run still marks the output")

    def test_a_passing_test_run_does_not_forgive_an_earlier_failed_rebuild(self):
        self.build()
        self.document("build", env=self.env(FAKE_EXIT="1"))
        self.assertTrue(self.state().needs_revalidation)
        result, document = self.document("test", "brave_unit_tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.state().needs_revalidation, "a passing test does not prove the browser output")
        result, document = self.run_app("--artifact", str(self.output_app()))
        self.assertNotIn("freshness", document["data"]["run"])
        self.build()
        self.assertFalse(self.state().needs_revalidation, "only a validated build clears it")
        self.document("test", "brave_unit_tests")
        self.assertFalse(self.state().needs_revalidation)

    def test_unusable_output_after_a_failed_rebuild_is_rejected(self):
        self.build()
        self.document("build", env=self.env(FAKE_EXIT="1"))
        os.unlink(self.output_app() / "Contents" / "Info.plist")
        result, document = self.run_app("--artifact", str(self.output_app()))
        self.assertEqual(document["error"]["code"], "ARTIFACT_MISMATCH")

    def test_failure_before_any_output_change_keeps_the_earlier_record(self):
        self.build()
        (self.src / "base" / "BUILD.gn").write_text("a local edit\n")
        result, document = self.document("build")
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertFalse(self.state().needs_revalidation)
        self.assertEqual(len([r for r in self.node_calls() if "build" in r["argv"]]), 1)

    def test_cancelled_build_keeps_the_marker_and_finishes_its_record(self):
        self.build()
        process = subprocess.Popen(
            [str(SCRIPTS / "bcore"), "--json", "--config", str(self.sandbox.config), "--checkout", "main", "build"],
            env=self.env(FAKE_SLEEP="60"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while len([r for r in self.node_calls() if "build" in r["argv"]]) < 2 and time.time() < deadline:
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=30)
        document = json.loads(stdout)
        self.assertEqual((process.returncode, document["status"]), (143, "cancelled"))
        self.assertTrue(self.state().needs_revalidation)
        self.assertEqual(self.state().last_attempt()["outcome"], "cancelled")

    def test_cancelled_build_leaves_no_descendant_running_even_if_it_ignores_term(self):
        pidfile = self.sandbox.root / "descendant-pid"
        hook = self.sandbox.hook("""
import subprocess, time
subprocess.Popen([sys.executable, "-c", "import os,signal,sys,time\\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
                  "open(sys.argv[1], 'w').write(str(os.getpid()))\\ntime.sleep(120)", os.environ["FAKE_DESCENDANT_PID"]])
time.sleep(120)
""")
        process = subprocess.Popen(
            [str(SCRIPTS / "bcore"), "--json", "--config", str(self.sandbox.config), "--checkout", "main", "build"],
            env=self.sandbox.env(FAKE_HOOK=hook, FAKE_DESCENDANT_PID=str(pidfile)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        descendant = int(pidfile.read_text())
        self.addCleanup(lambda: alive(descendant) and os.kill(descendant, signal.SIGKILL))
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=60)
        document = json.loads(stdout)
        self.assertEqual((process.returncode, document["status"]), (143, "cancelled"))
        self.assertFalse(alive(descendant), "the process this command started was stopped")
        self.assertNotIn("cleanup_incomplete", document["error"]["details"])

    def test_interrupted_operation_without_cleanup_is_reported_as_uncertain_later(self):
        self.build()
        process = subprocess.Popen(
            [str(SCRIPTS / "bcore"), "--json", "--config", str(self.sandbox.config), "--checkout", "main", "build"],
            env=self.env(FAKE_SLEEP="60"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while len([r for r in self.node_calls() if "build" in r["argv"]]) < 2 and time.time() < deadline:
            time.sleep(0.05)
        child = self.node_calls()[-1]["pid"]
        process.kill()
        os.kill(child, signal.SIGKILL)
        process.communicate()
        from scaffold.brave.records import incomplete_operations
        self.assertEqual(len(incomplete_operations(self.sandbox.config.parent, self.core)), 1)
        self.assertTrue(self.state().needs_revalidation)
        result, document = self.run_app("--artifact", str(self.output_app()))
        self.assertEqual(result.returncode, 0, result.stderr)
        result, document = self.document("context")
        self.assertEqual([w["code"] for w in document["warnings"]], ["INCOMPLETE_OPERATION"])
        self.assertEqual(len(document["data"]["incomplete_operations"]), 1)


class RestartEscalationTests(unittest.TestCase):
    """Shutdown escalation against a real process that ignores polite requests."""

    def test_unresponsive_application_is_terminated_and_a_survivor_is_reported(self):
        import tempfile
        from scaffold.brave import macos
        from scaffold.common.results import ScaffoldError
        from tests.support import make_app
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        app = make_app(directory)
        binary = os.path.join(app, "Contents", "MacOS", "Brave Browser Development")
        process = subprocess.Popen([binary, "i"], start_new_session=True)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        bundle = macos.read_bundle(app)
        env = {"PATH": "/usr/bin:/bin"}
        instances = macos.running_instances(bundle, env)
        self.assertEqual([item["pid"] for item in instances], [process.pid])
        with mock.patch.object(macos, "QUIT_WAIT_SECONDS", 0.3), mock.patch.object(macos, "TERM_WAIT_SECONDS", 0.5), \
                mock.patch.object(macos, "KILL_WAIT_SECONDS", 3):
            steps = macos.stop_instances(bundle, instances, env)
        self.assertEqual(steps, ["quit", "terminate", "kill"])
        self.assertTrue(wait_gone(process.pid))
        survivor = mock.Mock(pid=os.getpid())
        with mock.patch.object(macos, "QUIT_WAIT_SECONDS", 0.1), mock.patch.object(macos, "TERM_WAIT_SECONDS", 0.1), \
                mock.patch.object(macos, "KILL_WAIT_SECONDS", 0.1), mock.patch.object(macos.os, "kill") as kill:
            kill.side_effect = lambda pid, sig: None
            with self.assertRaises(ScaffoldError) as caught:
                macos.stop_instances(bundle, [{"pid": os.getpid(), "bundle": app}], env)
        self.assertEqual(caught.exception.code, "LAUNCH_FAILED")


class RestartLoggingTests(unittest.TestCase):
    """Every probe the restart dispatches is logged; repeated polling has a bounded representation."""

    def test_liveness_and_launch_probes_are_logged_with_polling_summarised(self):
        import io
        import tempfile
        from scaffold.brave import macos
        from scaffold.common.procs import CommandLog
        from tests.support import make_app
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        app = make_app(directory, bundle_id="com.brave.RestartLogging")
        binary = os.path.join(app, "Contents", "MacOS", "Brave Browser Development")
        process = subprocess.Popen([binary, "i"], start_new_session=True)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        bundle = macos.read_bundle(app)
        stream = io.StringIO()
        log = CommandLog(stream=stream)
        env = {"PATH": "/usr/bin:/bin"}
        instances = macos.running_instances(bundle, env, log)
        with mock.patch.object(macos, "QUIT_WAIT_SECONDS", 1.0), mock.patch.object(macos, "TERM_WAIT_SECONDS", 1.0), \
                mock.patch.object(macos, "KILL_WAIT_SECONDS", 5):
            macos.stop_instances(bundle, instances, env, log)
        probes = [entry for entry in log.records if entry["argv"][:3] == ["ps", "-o", "stat="]]
        self.assertEqual(len(probes), 1, "the same liveness probe is described once")
        self.assertGreater(probes[0]["repeated"], 3, "and says how often it ran")
        self.assertEqual(stream.getvalue().count("ps -o stat="), 1)
        self.assertIn("more times", stream.getvalue())
        fake_open = os.path.join(directory, "bin")
        os.makedirs(fake_open)
        with open(os.path.join(fake_open, "open"), "w") as stream:
            stream.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(fake_open, "open"), 0o755)
        with mock.patch.object(macos, "LAUNCH_WAIT_SECONDS", 1.0):
            with self.assertRaises(Exception):
                macos.launch({**bundle, "path": os.path.join(directory, "absent.app")},
                             {"PATH": fake_open + ":/usr/bin:/bin"}, log)
        listings = [entry for entry in log.records if entry["argv"][:2] == ["ps", "-axo"]]
        self.assertEqual(len(listings), 2, "the initial listing, and one description of the launch polling")
        self.assertGreater(listings[-1]["repeated"], 1)


if __name__ == "__main__":
    unittest.main()
