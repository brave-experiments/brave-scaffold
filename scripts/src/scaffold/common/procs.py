# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Subprocess execution with command logging, redaction, and cancellation."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .redaction import redact_argv
from .results import Cancelled

TERMINATE_GRACE_SECONDS = 10
KILL_WAIT_SECONDS = 3
PIPE_DRAIN_SECONDS = 3
CANCEL_SIGNALS = {130: signal.SIGINT, 143: signal.SIGTERM}


def format_command_block(argv, cwd):
    quoted = " ".join(shlex.quote(part) for part in redact_argv(argv))
    return "\n".join([
        "---------------------------",
        "Current directory: %s" % os.path.abspath(cwd),
        "Command:",
        quoted,
        "---------------------------",
    ])


@dataclass
class CommandLog:
    """Where command blocks go. Records every dispatched command."""

    enabled: bool = True
    stream: object = None
    records: list = field(default_factory=list)
    listeners: list = field(default_factory=list)

    def record(self, argv, cwd):
        """Note a command before it starts; listeners (such as an operation record) see the redacted form."""
        redacted = redact_argv(argv)
        entry = {"argv": redacted, "cwd": os.path.abspath(cwd)}
        self.records.append(entry)
        for listener in list(self.listeners):
            listener(entry)
        if self.enabled:
            stream = self.stream or sys.stderr
            stream.write(format_command_block(argv, cwd) + "\n")
            stream.flush()


@dataclass
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cleanup_incomplete: bool = False


def _forward_and_wait(process, timeout=None):
    """Wait for an owned process group; on cancellation forward the received signal and clean up within a bound."""
    try:
        return process.wait(timeout=timeout)
    except Cancelled as cancelled:
        cancelled.cleanup_incomplete = not terminate_group(process, signum=CANCEL_SIGNALS.get(cancelled.exit_code,
                                                                                              signal.SIGTERM))
        raise cancelled
    except subprocess.TimeoutExpired:
        terminate_group(process)
        raise


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_for_group(process, seconds):
    """Reap the leader and wait until no member of its group remains."""
    deadline = time.monotonic() + seconds
    while True:
        process.poll()
        if not _group_alive(process.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def terminate_group(process, grace=None, signum=signal.SIGTERM):
    """Stop every process started in the group `process` leads, even after the leader exited.

    Sends `signum`, waits up to `grace` seconds for the whole group to leave, then kills survivors and waits
    a short bounded time. Returns True when no member remains. The group id stays reserved while any member
    runs, so only processes this scaffold started are signalled.
    """
    grace = TERMINATE_GRACE_SECONDS if grace is None else grace
    process.poll()
    if not _group_alive(process.pid):
        return True
    _signal_group(process.pid, signum)
    if _wait_for_group(process, grace):
        return True
    _signal_group(process.pid, signal.SIGKILL)
    return _wait_for_group(process, KILL_WAIT_SECONDS)


def run_streaming(argv, cwd, env, log, json_mode=False, stdin=None):
    """Run a command whose output belongs to the user; return its exit code.

    In JSON mode the child's stdout goes to stderr so the result document is the
    only thing on stdout.
    """
    log.record(argv, cwd)
    stdout = sys.stderr if json_mode else None
    process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=stdout, stdin=stdin,
                               start_new_session=True)
    return _forward_and_wait(process)


def _drain(process):
    """Output already written by a finished or terminated probe; pipes held by escaped processes are abandoned."""
    try:
        out, err = process.communicate(timeout=PIPE_DRAIN_SECONDS)
        return out, err, True
    except subprocess.TimeoutExpired as expired:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        return expired.stdout or b"", expired.stderr or b"", False


def run_capture(argv, cwd, env, log=None, timeout=60, max_bytes=1_000_000):
    """Run a probe and capture bounded output. Logged when a log is supplied."""
    if log is not None:
        log.record(argv, cwd)
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        return ProcessResult(returncode=127, stderr=str(error))
    timed_out = incomplete = False
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        group_gone = terminate_group(process)
        out, err, drained = _drain(process)
        incomplete = not (group_gone and drained)
    except Cancelled as cancelled:
        cancelled.cleanup_incomplete = not terminate_group(
            process, signum=CANCEL_SIGNALS.get(cancelled.exit_code, signal.SIGTERM))
        raise
    return ProcessResult(returncode=124 if timed_out else process.returncode,
                         stdout=out[:max_bytes].decode("utf-8", "replace"),
                         stderr=err[:max_bytes].decode("utf-8", "replace"), timed_out=timed_out,
                         cleanup_incomplete=incomplete)


def install_signal_handlers():
    def handler(signum, _frame):
        raise Cancelled(130 if signum == signal.SIGINT else 143)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
