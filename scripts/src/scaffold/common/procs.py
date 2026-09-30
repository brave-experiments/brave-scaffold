# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Subprocess execution with command logging, redaction, and cancellation."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field

from .results import Cancelled

SECRET_NAME = re.compile(
    r"(token|secret|passw(?:or)?d|api[-_]?key|auth(?:orization)?|credential|private[-_]?key)",
    re.IGNORECASE)
URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/@\s:]+):(?P<secret>[^/@\s]+)@",
                             re.IGNORECASE)
REDACTED = "***"
TERMINATE_GRACE_SECONDS = 10


def redact_argv(argv):
    """Hide secret values in separated and --key=value forms and in URLs."""
    result = []
    hide_next = False
    for part in argv:
        part = str(part)
        if hide_next:
            result.append(REDACTED)
            hide_next = False
            continue
        if part.startswith("-") and "=" in part:
            name, _, value = part.partition("=")
            if SECRET_NAME.search(name):
                result.append("%s=%s" % (name, REDACTED))
                continue
        elif part.startswith("-") and SECRET_NAME.search(part):
            result.append(part)
            hide_next = True
            continue
        elif "=" in part and not part.startswith("-") and SECRET_NAME.search(part.partition("=")[0]) \
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part.partition("=")[0]):
            result.append("%s=%s" % (part.partition("=")[0], REDACTED))
            continue
        result.append(URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:" + REDACTED + "@", part))
    return result


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

    def record(self, argv, cwd):
        redacted = redact_argv(argv)
        self.records.append({"argv": redacted, "cwd": os.path.abspath(cwd)})
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


def _forward_and_wait(process, timeout=None):
    """Wait for an owned process group; on cancellation terminate it within a bound."""
    try:
        return process.wait(timeout=timeout)
    except Cancelled as cancelled:
        terminate_group(process)
        raise cancelled
    except subprocess.TimeoutExpired:
        terminate_group(process)
        raise


def terminate_group(process, grace=TERMINATE_GRACE_SECONDS):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()


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


def run_capture(argv, cwd, env, log=None, timeout=60, max_bytes=1_000_000):
    """Run a probe and capture bounded output. Logged when a log is supplied."""
    if log is not None:
        log.record(argv, cwd)
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        return ProcessResult(returncode=127, stderr=str(error))
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_group(process)
        out, err = process.communicate()
        return ProcessResult(returncode=124, stdout=out[:max_bytes].decode("utf-8", "replace"),
                             stderr=err[:max_bytes].decode("utf-8", "replace"), timed_out=True)
    except Cancelled:
        terminate_group(process)
        raise
    return ProcessResult(returncode=process.returncode,
                         stdout=out[:max_bytes].decode("utf-8", "replace"),
                         stderr=err[:max_bytes].decode("utf-8", "replace"))


def install_signal_handlers():
    def handler(signum, _frame):
        raise Cancelled(130 if signum == signal.SIGINT else 143)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
