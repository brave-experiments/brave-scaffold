# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Owned process groups: cancellation and timeouts must not leave descendants running."""

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
from scaffold.common.results import Cancelled

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
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() \
        not in ("", "Z")


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


if __name__ == "__main__":
    unittest.main()
