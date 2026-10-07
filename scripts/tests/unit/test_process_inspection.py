# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""A process inspection that failed is unknown, never an empty answer."""

import os
import signal
import subprocess
import sys
import unittest
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import macos
from scaffold.common.procs import CommandLog, ProcessResult
from scaffold.common.results import ScaffoldError

BUNDLE = {"bundle_identifier": "com.brave.ScaffoldTest.inspection", "name": "Example"}


class ListingTests(unittest.TestCase):
    def test_a_listing_that_failed_or_is_incomplete_is_an_error_not_an_empty_list(self):
        for label, result in (("nonzero exit", ProcessResult(returncode=1)),
                              ("killed", ProcessResult(returncode=-9)),
                              ("timed out", ProcessResult(returncode=0, timed_out=True)),
                              ("truncated", ProcessResult(returncode=0, stdout="1 /x.app/Contents/MacOS/y\n",
                                                          truncated=True))):
            with self.subTest(label), mock.patch.object(macos, "run_capture", return_value=result):
                with self.assertRaises(ScaffoldError) as caught:
                    macos.running_instances(BUNDLE, {})
                self.assertEqual(caught.exception.code, "LAUNCH_FAILED")
                self.assertIn("nothing was stopped or launched", caught.exception.message)

    def test_a_successful_empty_listing_is_an_empty_list(self):
        with mock.patch.object(macos, "run_capture", return_value=ProcessResult(returncode=0, stdout="")):
            self.assertEqual(macos.running_instances(BUNDLE, {}), [])


class ExitVerificationTests(unittest.TestCase):
    def setUp(self):
        self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                        start_new_session=True)
        self.addCleanup(lambda: (self.process.kill(), self.process.wait()))

    def test_a_failed_liveness_probe_does_not_prove_the_process_gone(self):
        for label, result in (("nonzero exit", ProcessResult(returncode=1)),
                              ("timed out", ProcessResult(returncode=0, stdout="S\n", timed_out=True)),
                              ("empty answer", ProcessResult(returncode=0, stdout=""))):
            with self.subTest(label), mock.patch.object(macos, "run_capture", return_value=result):
                self.assertTrue(macos._alive(self.process.pid))

    def test_only_the_process_vanishing_proves_it_gone_when_the_probe_fails(self):
        pid = self.process.pid
        self.process.kill()
        self.process.wait()
        with mock.patch.object(macos, "run_capture", return_value=ProcessResult(returncode=1)):
            self.assertFalse(macos._alive(pid))

    def test_a_zombie_counts_as_gone_when_the_probe_says_so(self):
        with mock.patch.object(macos, "run_capture", return_value=ProcessResult(returncode=0, stdout="Z+\n")):
            self.assertFalse(macos._alive(self.process.pid))

    def test_shutdown_is_not_reported_verified_when_liveness_cannot_be_inspected(self):
        def fake(argv, cwd, env, log=None, **kwargs):
            return ProcessResult(returncode=1 if argv[0] == "ps" else 0)
        # The process is killed for real by the escalation; only its inspection is broken.
        with mock.patch.object(macos, "run_capture", side_effect=fake), \
                mock.patch.object(macos, "QUIT_WAIT_SECONDS", 0.3), mock.patch.object(macos, "TERM_WAIT_SECONDS", 0.3), \
                mock.patch.object(macos, "KILL_WAIT_SECONDS", 0.3):
            with self.assertRaises(ScaffoldError) as caught:
                macos.stop_instances(BUNDLE, [{"pid": self.process.pid, "bundle": "/x.app"}], {}, CommandLog(enabled=False))
        self.assertEqual(caught.exception.code, "LAUNCH_FAILED")
        os.kill(self.process.pid, signal.SIGKILL)


class ForeignProcessTests(unittest.TestCase):
    def test_a_process_this_user_may_not_signal_is_reported_not_crashed_on(self):
        denied = []

        def kill(pid, sig):
            denied.append((pid, sig))
            raise PermissionError(1, "Operation not permitted")

        with mock.patch.object(macos, "run_capture", return_value=ProcessResult(returncode=0)), \
                mock.patch.object(macos.os, "kill", side_effect=kill), \
                mock.patch.object(macos, "_alive", return_value=True), \
                mock.patch.object(macos, "QUIT_WAIT_SECONDS", 0.2), mock.patch.object(macos, "TERM_WAIT_SECONDS", 0.2), \
                mock.patch.object(macos, "KILL_WAIT_SECONDS", 0.2):
            with self.assertRaises(ScaffoldError) as caught:
                macos.stop_instances(BUNDLE, [{"pid": 4242, "bundle": "/x.app"}], {}, CommandLog(enabled=False))
        self.assertEqual(caught.exception.code, "LAUNCH_FAILED")
        self.assertEqual(caught.exception.details["not_permitted"], [4242])
        self.assertIn("not permitted", caught.exception.message)
        self.assertEqual([sig for _, sig in denied], [signal.SIGTERM, signal.SIGKILL], "escalation still ran")


if __name__ == "__main__":
    unittest.main()
