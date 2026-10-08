# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Owned process groups: cancellation and timeouts must not leave descendants running."""

import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from scaffold.common import procs
from scaffold.common.results import Cancelled, ScaffoldError

STUBBORN = ("import os,signal,sys,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)")
SIGINT_AWARE = ("import os,signal,sys,time\n"
                "def on_int(*args):\n    open(sys.argv[1] + '.sigint', 'w').write('x')\n    os._exit(0)\n"
                "signal.signal(signal.SIGINT, on_int)\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)")
LEADER = ("import os,subprocess,sys,time\n"
          "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], stderr=subprocess.DEVNULL)\n"
          "while not os.path.exists(sys.argv[2]):\n    time.sleep(0.02)\n%s\n")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return state != "" and not state.startswith("Z")


def wait_for(path, seconds=10):
    deadline = time.time() + seconds
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.02)
    return os.path.exists(path)


class GroupTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="scaffold-procs-")
        self.cleanup_pids = []
        self.addCleanup(self.reap)

    def reap(self):
        for pid in self.cleanup_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def start(self, leader_tail, child_code=STUBBORN):
        """A process group whose leader starts one descendant, then behaves as leader_tail says."""
        marker = os.path.join(self.directory, "child-pid")
        leader = LEADER % leader_tail
        process = subprocess.Popen([sys.executable, "-c", leader, child_code, marker], start_new_session=True,
                                   stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait()))
        self.assertTrue(wait_for(marker), "the descendant did not start")
        pid = int(Path(marker).read_text())
        self.cleanup_pids.append(pid)
        return process, pid


class GitEnvironmentTests(unittest.TestCase):
    """A caller's Git repository selectors must never redirect Git into a different repository."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target, self.other = self.root / "target", self.root / "other"
        for repo in (self.target, self.other):
            repo.mkdir()
            for args in (["init", "-q", "-b", repo.name], ["config", "user.email", "t@example.com"],
                         ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"]):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            (repo / "file").write_text(repo.name)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", repo.name], check=True, capture_output=True)
        self.selectors = {"GIT_DIR": str(self.other / ".git"), "GIT_WORK_TREE": str(self.other)}

    def test_only_the_repository_selectors_are_removed(self):
        environ = {**self.selectors, "GIT_INDEX_FILE": "x", "GIT_COMMON_DIR": "y", "GIT_OBJECT_DIRECTORY": "z",
                   "GIT_ALTERNATE_OBJECT_DIRECTORIES": "a", "GIT_PREFIX": "p", "GIT_NAMESPACE": "n",
                   "GIT_SSH_COMMAND": "ssh -i key", "GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin", "HOME": "/h"}
        self.assertEqual(procs.git_environment(environ),
                         {"GIT_SSH_COMMAND": "ssh -i key", "GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin", "HOME": "/h"})
        with mock.patch.dict(os.environ, self.selectors):
            self.assertNotIn("GIT_DIR", procs.git_environment())
            self.assertIn("PATH", procs.git_environment())

    def test_a_probe_asked_about_one_repository_is_never_answered_by_another(self):
        for label, env in (("explicit environment", {**os.environ, **self.selectors}), ("inherited", None)):
            with self.subTest(label), mock.patch.dict(os.environ, self.selectors if env is None else {}):
                result = procs.run_capture(["git", "-C", str(self.target), "branch", "--show-current"],
                                           str(self.target), env)
                self.assertEqual(result.stdout.strip(), "target")

    def test_a_streamed_command_is_not_redirected_either(self):
        with tempfile.TemporaryDirectory() as directory:
            log = procs.CommandLog(stream=io.StringIO(), verbosity="normal")
            log.open(directory)
            try:
                code = procs.run_streaming(["git", "-C", str(self.target), "branch", "--show-current"], str(self.target),
                                           {**os.environ, **self.selectors}, log, json_mode=True)
                saved = Path(log.path).read_text()
            finally:
                log.close()
        self.assertEqual(code, 0)
        self.assertIn("\ntarget\n", saved, "the branch of the repository that was asked about")
        self.assertNotIn("\nother\n", saved, "never the branch of the repository the environment pointed at")

    def test_other_programs_keep_their_environment(self):
        result = procs.run_capture(["sh", "-c", 'printf %s "$GIT_DIR"'], str(self.root), {**os.environ, **self.selectors})
        self.assertEqual(result.stdout, self.selectors["GIT_DIR"])


class SpawnFailureTests(unittest.TestCase):
    def test_a_command_that_cannot_start_exits_127_and_is_logged_like_any_other_failure(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive), tempfile.TemporaryDirectory() as directory:
                stream = io.StringIO()
                log = procs.CommandLog(stream=stream, verbosity="normal")
                log.open(directory)
                try:
                    code = procs.run_streaming(["/nonexistent/scaffold-tool", "--flag"], directory, os.environ, log,
                                               json_mode=True, interactive=interactive)
                    saved = Path(log.path).read_text()
                finally:
                    log.close()
                self.assertEqual(code, 127)
                self.assertIn("/nonexistent/scaffold-tool", saved)
                self.assertIn("Child exit: 127", saved)
                self.assertIn("could not be started", stream.getvalue())


class TerminateGroupTests(GroupTestCase):
    def test_a_descendant_that_ignores_term_is_killed_after_the_leader_exits(self):
        process, child = self.start("import signal\nsignal.signal(signal.SIGTERM, lambda *args: sys.exit(0))\ntime.sleep(60)")
        self.assertTrue(procs.terminate_group(process, grace=0.5))
        self.assertFalse(alive(child))

    def test_survivors_are_found_after_the_leader_has_already_exited_and_been_reaped(self):
        process, child = self.start("sys.exit(0)")
        process.wait()
        self.assertTrue(alive(child))
        self.assertTrue(procs.terminate_group(process, grace=0.5))
        self.assertFalse(alive(child))

    def test_processes_outside_the_group_are_left_alone(self):
        bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        process, child = self.start("time.sleep(60)")
        procs.terminate_group(process, grace=0.5)
        self.assertIsNone(bystander.poll())

    def test_the_received_signal_is_the_one_forwarded(self):
        marker = os.path.join(self.directory, "child-pid")
        process, child = self.start("time.sleep(60)", SIGINT_AWARE)
        self.assertTrue(procs.terminate_group(process, grace=2, signum=signal.SIGINT))
        self.assertTrue(os.path.exists(marker + ".sigint"), "the descendant saw SIGINT, not only SIGKILL")

    def test_cancellation_forwards_the_signal_and_reports_incomplete_cleanup(self):
        process, child = self.start("time.sleep(60)", SIGINT_AWARE)
        with mock.patch.object(process, "wait", side_effect=Cancelled(130)):
            with self.assertRaises(Cancelled) as caught:
                procs._forward_and_wait(process)
        self.assertFalse(caught.exception.cleanup_incomplete)
        self.assertFalse(alive(child))
        process, child = self.start("time.sleep(60)", STUBBORN)
        with mock.patch.object(process, "wait", side_effect=Cancelled(143)), \
                mock.patch.object(procs, "TERMINATE_GRACE_SECONDS", 0.3), \
                mock.patch.object(procs, "_group_alive", return_value=True), mock.patch.object(procs, "KILL_WAIT_SECONDS", 0.2):
            with self.assertRaises(Cancelled) as caught:
                procs._forward_and_wait(process)
        self.assertTrue(caught.exception.cleanup_incomplete, "survivors that could not be confirmed gone are reported")


class CaptureTests(GroupTestCase):
    def test_a_probe_whose_descendant_holds_the_pipes_returns_after_its_timeout(self):
        started = time.monotonic()
        result = procs.run_capture(["sh", "-c", "sleep 30 & echo started"], self.directory, None, timeout=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result.timed_out)
        self.assertIn("started", result.stdout)
        self.assertFalse(result.cleanup_incomplete, "the owned descendant was terminated")

    def test_pipe_draining_is_bounded_when_a_process_escaped_the_group(self):
        code = ("import subprocess,sys; p = subprocess.Popen(['sleep','30'], start_new_session=True); "
                "print(p.pid, flush=True)")
        started = time.monotonic()
        with mock.patch.object(procs, "PIPE_DRAIN_SECONDS", 0.5):
            result = procs.run_capture([sys.executable, "-c", code], self.directory, None, timeout=1)
        escaped = int(result.stdout.split()[0])
        self.cleanup_pids.append(escaped)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.cleanup_incomplete, "output pipes were abandoned")

    def test_a_probe_that_timed_out_or_was_cut_short_says_so_in_the_command_record(self):
        log = procs.CommandLog(enabled=False)
        with mock.patch.object(procs, "PIPE_DRAIN_SECONDS", 0.3):
            procs.run_capture(["sh", "-c", "sleep 30 & echo started"], self.directory, None, log, timeout=1)
            procs.run_capture([sys.executable, "-c", "print('x' * 5000)"], self.directory, None, log, max_bytes=10)
            procs.run_capture(["true"], self.directory, None, log)
        timed_out, cut, plain = log.records
        self.assertEqual((timed_out.get("timed_out"), cut.get("truncated")), (True, True))
        self.assertNotIn("timed_out", plain)
        self.assertNotIn("truncated", plain)


class BoundedCaptureTests(unittest.TestCase):
    def test_a_chatty_child_cannot_use_more_memory_than_the_limit(self):
        import tracemalloc
        code = "import sys\nfor _ in range(100):\n    sys.stdout.buffer.write(b'x' * 1_000_000)\n"
        tracemalloc.start()
        try:
            result = procs.run_capture([sys.executable, "-c", code], os.getcwd(), None, max_bytes=1000, timeout=60)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual((len(result.stdout), result.returncode, result.truncated), (1000, 0, True))
        self.assertLess(peak, 20_000_000, "output beyond the limit is discarded while reading, not kept")

    def test_output_within_the_limit_is_complete_and_not_marked(self):
        result = procs.run_capture([sys.executable, "-c", "print('ok'); import sys; print('err', file=sys.stderr)"],
                                   os.getcwd(), None, max_bytes=1000)
        self.assertEqual((result.stdout, result.stderr, result.truncated), ("ok\n", "err\n", False))

    def test_each_stream_is_limited_separately(self):
        code = "import sys\nsys.stdout.write('o' * 5000)\nsys.stderr.write('e' * 5000)\n"
        result = procs.run_capture([sys.executable, "-c", code], os.getcwd(), None, max_bytes=100)
        self.assertEqual((len(result.stdout), len(result.stderr), result.truncated), (100, 100, True))


class RepositoryLoggingTests(unittest.TestCase):
    def test_each_repository_command_has_a_full_block_and_record(self):
        import io
        stream = io.StringIO()
        log = procs.CommandLog(stream=stream)
        seen = []
        log.listeners.append(seen.append)
        for name in ("a", "b", "c"):
            log.record(["git", "-C", "/repo/" + name, "status"], "/repo/" + name)
        log.record(["git", "log"], "/repo")
        text = stream.getvalue()
        self.assertEqual(text.count("Command:"), 4, "every distinct command has its own full block")
        for name in ("a", "b", "c"):
            self.assertIn(procs.format_command_block(["git", "-C", "/repo/" + name, "status"],
                                                    "/repo/" + name), text)
        self.assertEqual(len(log.records), 4)
        self.assertEqual(len(seen), 4, "listeners such as the operation record see every command")


class TruncatedEvidenceTests(unittest.TestCase):
    """Partial command output is never treated as complete safety evidence."""

    def truncated(self, stdout=""):
        return procs.ProcessResult(returncode=0, stdout=stdout, truncated=True)

    def test_local_work_and_tracked_file_checks_fail_instead_of_answering(self):
        from scaffold.brave import gitstate
        with mock.patch.object(gitstate, "run_capture", return_value=self.truncated(" M a.cc\0")):
            with self.assertRaises(ScaffoldError):
                gitstate.changed_paths("/repo", ["a.cc"])
            with self.assertRaises(ScaffoldError):
                gitstate.tracked_paths("/repo", ["a.cc"])

    def test_freshness_inputs_become_unknown(self):
        from scaffold.brave import freshness
        with mock.patch.object(freshness, "run_capture", return_value=self.truncated("a.cc\0")):
            self.assertIsNone(freshness.tracked_changes_state("/repo"))
            self.assertIsNone(freshness.worktree_state("/repo"))

    def test_process_listing_and_large_file_listing_fail(self):
        from scaffold.brave import android_deps, macos
        with mock.patch.object(macos, "run_capture", return_value=self.truncated("1 x\n")):
            with self.assertRaises(ScaffoldError):
                macos.running_instances({"bundle_identifier": "com.brave.X"}, {})
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".gitattributes").write_text("*.bin filter=lfs\n")
            with mock.patch.object(android_deps, "_git", return_value=self.truncated("abc - big.bin\n")):
                with self.assertRaises(ScaffoldError):
                    android_deps.lfs_pointers(directory)


if __name__ == "__main__":
    unittest.main()
