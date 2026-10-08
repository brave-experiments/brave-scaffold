# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Interactive children retain terminal access and return foreground control on exit or cancellation."""

import os
import pty
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import tests.support
from scaffold.common import procs
from scaffold.common.results import Cancelled


class InteractiveShellTests(unittest.TestCase):
    def exercise_terminal(self, cancel=False, shell_job=False):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            pid, master = pty.fork()
            if pid == 0:
                try:
                    signal.alarm(10)
                    procs.TERMINATE_GRACE_SECONDS = .2
                    original_group = os.tcgetpgrp(0)
                    original_handler = signal.getsignal(signal.SIGTTOU)
                    code = (
                        "import os,signal,sys,time\n"
                        "signal.alarm(5)\n"
                        "with open('/dev/tty') as terminal:\n"
                        " assert os.tcgetpgrp(terminal.fileno()) == os.getpgrp()\n"
                        " print('CHILD_HAS_FOREGROUND_TTY', flush=True)\n"
                        " open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                        + (" while True: time.sleep(.01)\n" if cancel else
                           " assert terminal.readline().strip() == 'hello'\n"))
                    worker = None
                    argv = [sys.executable, "-c", code, str(marker)]
                    if shell_job:
                        argv = [shutil.which("zsh"), "-f", "-i", "-c",
                                'sleep 30 & echo $! > "$1"; echo CHILD_HAS_FOREGROUND_TTY; wait',
                                "zsh", str(marker)]
                    if cancel:
                        procs.install_signal_handlers()

                        def interrupt():
                            deadline = time.monotonic() + 4
                            while (not marker.exists() or not marker.read_text().strip()) and time.monotonic() < deadline:
                                time.sleep(.01)
                            os.kill(os.getpid(), signal.SIGTERM)

                        worker = threading.Thread(target=interrupt)
                        worker.start()
                    try:
                        result = procs.run_streaming(
                            argv, directory, dict(os.environ),
                            procs.CommandLog(enabled=False), interactive=True)
                        assert not cancel and result == 0
                    except Cancelled as error:
                        assert cancel and error.exit_code == 143 and error.cleanup_incomplete
                        if shell_job:
                            job = int(marker.read_text())
                            assert os.getpgid(job) != original_group
                            os.kill(job, 0)  # The separate job survives, so complete cleanup must not be claimed.
                    finally:
                        if worker:
                            worker.join(5)
                        if shell_job and marker.exists() and marker.read_text().strip():
                            try:
                                os.kill(int(marker.read_text()), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    assert os.tcgetpgrp(0) == original_group
                    assert signal.getsignal(signal.SIGTTOU) == original_handler
                    with open('/dev/tty'):
                        print('PARENT_REGAINED_TTY', flush=True)
                    os._exit(0)
                except BaseException:
                    import traceback
                    traceback.print_exc()
                    os._exit(1)
            output = bytearray()
            sent = False
            deadline = time.monotonic() + 15
            try:
                while time.monotonic() < deadline:
                    if not select.select([master], [], [], .1)[0]:
                        continue
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    if not cancel and not sent and b'CHILD_HAS_FOREGROUND_TTY' in output:
                        os.write(master, b'hello\n')
                        sent = True
                else:
                    os.kill(pid, signal.SIGKILL)
            finally:
                if shell_job and marker.exists() and marker.read_text().strip():
                    try:
                        os.kill(int(marker.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                os.close(master)
                _, status = os.waitpid(pid, 0)
            text = output.decode(errors='replace')
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, text)
            self.assertIn('CHILD_HAS_FOREGROUND_TTY', text)
            self.assertIn('PARENT_REGAINED_TTY', text)

    def test_terminal_input_and_foreground_restore_after_exit(self):
        self.exercise_terminal()

    def test_cancellation_stops_child_and_restores_foreground(self):
        self.exercise_terminal(cancel=True)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is required")
    def test_cancellation_reports_unverified_shell_job_cleanup(self):
        self.exercise_terminal(cancel=True, shell_job=True)

    def test_terminal_without_controlling_session_starts_child(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        code = (
            "import tests.support\n"
            "from scaffold.common import procs\n"
            "import os\n"
            "assert os.isatty(0)\n"
            "raise SystemExit(procs.run_streaming(['/bin/sh', '-c', 'exit 7'], '.', "
            "dict(os.environ), procs.CommandLog(enabled=False), interactive=True))\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=tests.support.SCRIPTS,
                                stdin=slave, capture_output=True, text=True,
                                start_new_session=True, timeout=5)
        self.assertEqual(result.returncode, 7, result.stderr)
