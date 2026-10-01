# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Interactive output must retain terminal detection and dimensions while logging."""
import contextlib
import io
import json
import os
import pty
import sys
import tempfile
import termios
import unittest
from pathlib import Path

import tests.support
from scaffold.common.procs import CommandLog, run_streaming


class TerminalOutputTests(unittest.TestCase):
    def invoke(self, json_mode=False, quiet=False, terminal=True):
        with tempfile.TemporaryDirectory() as directory:
            master, slave = pty.openpty()
            termios.tcsetwinsize(slave, (37, 111))
            self.addCleanup(os.close, master)
            console = os.fdopen(slave, 'w', buffering=1)
            self.addCleanup(console.close)
            stderr = console if terminal else io.StringIO()
            stdout = console if terminal else io.StringIO()
            log = CommandLog(stream=stderr, verbosity='quiet' if quiet else 'normal')
            log.open(directory)
            try:
                code = "import os,json; print(json.dumps({'tty':os.isatty(1) and os.isatty(2),'size':list(os.get_terminal_size(1)) if os.isatty(1) else None})); print('\\x1b[1mbold\\x1b[0m')"
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    rc = run_streaming([sys.executable, '-c', code], directory, os.environ, log, json_mode=json_mode)
                saved = Path(log.path).read_text()
                line = next(line for line in saved.splitlines() if line.startswith('{"tty"'))
                if terminal:
                    os.set_blocking(master, False)
                    captured = bytearray()
                    while True:
                        try:
                            captured.extend(os.read(master, 65536))
                        except BlockingIOError:
                            break
                    console_text = captured.decode('utf-8')
                else:
                    console_text = stdout.getvalue()
                return rc, json.loads(line), saved, console_text
            finally:
                log.close()

    def test_interactive_children_keep_terminal_detection_size_and_style(self):
        rc, child, saved, console = self.invoke()
        self.assertEqual(rc, 0)
        self.assertEqual(child, {'tty': True, 'size': [111, 37]})
        self.assertIn('\x1b[1mbold\x1b[0m', saved)
        self.assertIn('\x1b[1mbold\x1b[0m', console)

    def test_quiet_does_not_change_interactive_child_mode(self):
        _, child, _, console = self.invoke(quiet=True)
        self.assertTrue(child['tty'])
        self.assertEqual(console, '')

    def test_redirected_and_json_output_stay_noninteractive(self):
        self.assertFalse(self.invoke(terminal=False)[1]['tty'])
        self.assertFalse(self.invoke(json_mode=True)[1]['tty'])

    def test_resize_reaches_child_and_restores_signal_handler(self):
        import signal
        import threading
        import time
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ready'
            master, slave = pty.openpty()
            console = os.fdopen(slave, 'w', buffering=1)
            self.addCleanup(os.close, master)
            self.addCleanup(console.close)
            log = CommandLog(stream=console, verbosity='quiet')
            log.open(directory)
            previous = signal.getsignal(signal.SIGWINCH)
            code = ("import os,signal,sys,time\n"
                    "def resized(*args):\n print('SIZE', list(os.get_terminal_size(1)), flush=True); sys.exit(0)\n"
                    "signal.signal(signal.SIGWINCH,resized)\nsignal.alarm(5)\n"
                    "open(sys.argv[1],'w').write('ready')\nwhile True: time.sleep(.01)\n")
            def change_size():
                deadline = time.monotonic() + 4
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                if marker.exists():
                    termios.tcsetwinsize(console.fileno(), (45, 132))
                    os.kill(os.getpid(), signal.SIGWINCH)
            worker = threading.Thread(target=change_size)
            worker.start()
            try:
                with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                    rc = run_streaming([sys.executable, '-c', code, str(marker)], directory, os.environ, log)
                self.assertEqual(rc, 0)
                self.assertIn('SIZE [132, 45]', Path(log.path).read_text())
                self.assertEqual(signal.getsignal(signal.SIGWINCH), previous)
            finally:
                worker.join(5)
                log.close()

    def test_cancelling_a_terminal_child_stops_its_group(self):
        import signal
        import threading
        import time
        from scaffold.common import procs
        from scaffold.common.results import Cancelled
        from tests.unit.test_procs import alive
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ready'
            master, slave = pty.openpty()
            console = os.fdopen(slave, 'w', buffering=1)
            self.addCleanup(os.close, master)
            self.addCleanup(console.close)
            log = CommandLog(stream=console, verbosity='quiet')
            old_int, old_term = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
            old_resize = signal.getsignal(signal.SIGWINCH)
            code = ("import os,signal,sys,time\nsignal.alarm(5)\n"
                    "open(sys.argv[1],'w').write(str(os.getpid()))\nwhile True: time.sleep(.01)")
            def cancel():
                deadline = time.monotonic() + 4
                while (not marker.exists() or not marker.read_text()) and time.monotonic() < deadline:
                    time.sleep(.01)
                if marker.exists() and marker.read_text():
                    os.kill(os.getpid(), signal.SIGTERM)
            worker = threading.Thread(target=cancel)
            procs.install_signal_handlers()
            worker.start()
            try:
                with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                    with self.assertRaises(Cancelled) as caught:
                        run_streaming([sys.executable, '-c', code, str(marker)], directory, os.environ, log)
                self.assertEqual(caught.exception.exit_code, 143)
                self.assertFalse(caught.exception.cleanup_incomplete)
                self.assertFalse(alive(int(marker.read_text())))
                self.assertEqual(signal.getsignal(signal.SIGWINCH), old_resize)
            finally:
                worker.join(5)
                signal.signal(signal.SIGINT, old_int)
                signal.signal(signal.SIGTERM, old_term)
