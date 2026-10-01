# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Subprocess execution with command logging, redaction, and cancellation."""

from __future__ import annotations

import codecs
import os
import re
import tempfile
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .redaction import SECRET_NAME, URL_CREDENTIALS, redact_argv, redact_url_credentials
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
    polled: dict = field(default_factory=dict)
    verbosity: str = "verbose"
    diagnostic: object = None
    path: str | None = None
    progress_at: float = 0
    progress_line: bool = False

    def open(self, root):
        directory = Path(root) / ".bdev" / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        fd, self.path = tempfile.mkstemp(prefix=time.strftime("%Y%m%dT%H%M%S-"), suffix=".log", dir=directory)
        self.diagnostic = os.fdopen(fd, "w", encoding="utf-8")

    def save(self, text):
        if self.diagnostic:
            self.diagnostic.write(text)
            self.diagnostic.flush()

    def clear_progress(self):
        if self.progress_line:
            stream = self.stream or sys.stderr
            stream.write("\r\033[2K")
            stream.flush()
            self.progress_line = False

    def message(self, text):
        self.clear_progress()
        stream = self.stream or sys.stderr
        stream.write(text + "\n")
        stream.flush()

    def phase(self, text):
        self.save(text + "\n")
        if self.verbosity != "quiet":
            self.message(text)

    def progress(self, text):
        stream = self.stream or sys.stderr
        tty = stream.isatty()
        now = time.monotonic()
        if now - self.progress_at >= (1 if tty else 10):
            self.save(text + "\n")
            if self.verbosity != "quiet":
                if tty:
                    stream.write("\r\033[2K" + text)
                    stream.flush()
                    self.progress_line = True
                else:
                    self.message(text)
            self.progress_at = now

    def close(self):
        self.clear_progress()
        if self.diagnostic:
            self.diagnostic.close()
            self.diagnostic = None

    def record(self, argv, cwd, poll=False, primary=False, display_argv=None):
        """Note a command before it starts; listeners (such as an operation record) see the redacted form.

        A `poll` command that repeats while waiting is described once; later runs only raise its `repeated`
        count, which `finish_polls` reports, so a wait loop cannot flood the log.
        """
        key = (tuple(str(part) for part in argv), os.path.abspath(cwd))
        if poll and key in self.polled:
            self.polled[key]["repeated"] = self.polled[key].get("repeated", 0) + 1
            return None
        redacted = redact_argv(argv)
        entry = {"argv": redacted, "cwd": os.path.abspath(cwd)}
        if poll:
            self.polled[key] = entry
        self.records.append(entry)
        for listener in list(self.listeners):
            listener(entry)
        block = format_command_block(argv, cwd)
        self.save(block + "\n")
        if self.enabled and self.verbosity != "quiet" and (primary or self.verbosity == "verbose"):
            self.message(block if self.verbosity == "verbose" else
                         "Current directory: %s\n$ %s" % (os.path.abspath(cwd), shlex.join(redact_argv(display_argv) if display_argv else redacted)))
        return entry

    def finish_polls(self):
        """Say how often each polled command ran again after its first description."""
        for entry in self.polled.values():
            if entry.get("repeated"):
                text = "(command repeated %d more times: %s)" % (entry["repeated"], shlex.join(entry["argv"]))
                self.save(text + "\n")
                if self.enabled and self.verbosity == "verbose":
                    self.message("(the command above ran %d more times while waiting)" % entry["repeated"])
        self.polled.clear()


@dataclass
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cleanup_incomplete: bool = False
    truncated: bool = False


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


def run_streaming(argv, cwd, env, log, json_mode=False, stdin=None, preserve_stdout=False, display_argv=None,
                  interactive=False):
    """Run a command whose output belongs to the user; return its exit code.

    In JSON mode the child's stdout goes to stderr so the result document is the
    only thing on stdout.
    """
    log.record(argv, cwd, primary=True, display_argv=display_argv)
    if interactive:
        # Shell prompts and terminal control need inherited descriptors, not a text tee.
        log.save("Interactive shell output uses the terminal directly and is not captured.\n")
        process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=stdin,
                                   stdout=sys.stderr if json_mode else None, start_new_session=True)
        return _forward_and_wait(process)
    process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=stdin, start_new_session=True)
    output = _StreamOutput(log, argv, env, process.stdout, json_mode, preserve_stdout)
    code = None
    reader = _BoundedReader(process, 0, output.receive)
    try:
        # Poll the leader so an escaped descendant cannot hold these pipes open forever.
        while not reader.read(0.2):
            if process.poll() is not None:
                if not reader.read(PIPE_DRAIN_SECONDS):
                    warning = "Warning: child output pipes remained open after exit; capture stopped."
                    log.save(warning + "\n")
                    log.message(warning)
                break
        code = _forward_and_wait(process)
    except Cancelled as cancelled:
        cancelled.cleanup_incomplete = not terminate_group(
            process, signum=CANCEL_SIGNALS.get(cancelled.exit_code, signal.SIGTERM))
        reader.read(PIPE_DRAIN_SECONDS)
        raise
    except BaseException:
        terminate_group(process)
        raise
    finally:
        reader.close()
        output.finish()
        if code != 0 and log.verbosity == "quiet" and output.tail:
            log.message("Child output (last 40 lines, at most 16 KiB):\n" + output.tail)
    log.save("Child exit: %s\n" % code)
    return code


class _StreamOutput:
    """Redact complete lines before displaying or saving them, including split writes.

    A pathological line is omitted after 1 MiB rather than allowing unbounded memory or
    writing a partial secret. Ordinary newline and carriage-return progress stays live.
    """

    def __init__(self, log, argv, env, stdout, json_mode, preserve_stdout=False):
        self.preserve_stdout = preserve_stdout and not json_mode and log.verbosity != "quiet"
        self.log, self.stdout, self.json_mode = log, stdout, json_mode
        self.pending, self.decoders, self.discard = {}, {}, set()
        self.tail = ""
        secrets = {v for k, v in (env or os.environ).items() if v and SECRET_NAME.search(k)}
        hide_next = False
        for arg in map(str, argv):
            if hide_next:
                secrets.add(arg)
            hide_next = arg.startswith("-") and "=" not in arg and bool(SECRET_NAME.search(arg))
            if "=" in arg and SECRET_NAME.search(arg.partition("=")[0]):
                secrets.add(arg.partition("=")[2])
            secrets.update(match.group("secret") for match in URL_CREDENTIALS.finditer(arg))
        secrets.update(line for value in list(secrets) for line in value.splitlines() if line)
        self.secrets = sorted(filter(None, secrets), key=len, reverse=True)

    def receive(self, stream, chunk):
        if self.preserve_stdout and stream is self.stdout:
            # Direct tools may emit binary data or prompts without a newline.
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        decoder = self.decoders.setdefault(stream, codecs.getincrementaldecoder("utf-8")("replace"))
        text = self.pending.pop(stream, "") + decoder.decode(chunk)
        parts = re.split(r"([\n\r])", text)
        for index in range(0, len(parts) - 1, 2):
            line = parts[index] + parts[index + 1]
            if stream not in self.discard:
                if len(line.encode("utf-8")) > 1_048_576:
                    line = "[Output line exceeds 1 MiB; omitted.]\n"
                self.write(stream, line)
            self.discard.discard(stream)
        remaining = parts[-1]
        if len(remaining.encode("utf-8")) > 1_048_576:
            if stream not in self.discard:
                self.write(stream, "[Output line exceeds 1 MiB; omitted.]\n")
            self.discard.add(stream)
        elif stream not in self.discard:
            self.pending[stream] = remaining

    def write(self, stream, text):
        for secret in self.secrets:
            text = text.replace(secret, "***")
        text = redact_url_credentials(text)
        self.log.save(text)
        tail = (self.tail + text).encode("utf-8")[-16384:].decode("utf-8", "ignore")
        self.tail = "".join(tail.splitlines(keepends=True)[-40:])
        if self.log.verbosity != "quiet" and not (self.preserve_stdout and stream is self.stdout):
            self.log.clear_progress()
            destination = (self.log.stream or sys.stderr) if self.json_mode or stream is not self.stdout else sys.stdout
            destination.write(text)
            destination.flush()

    def finish(self):
        for stream, decoder in self.decoders.items():
            if stream not in self.discard:
                self.write(stream, self.pending.get(stream, "") + decoder.decode(b"", final=True))


class _BoundedReader:
    """Reads a process's stdout and stderr keeping at most `limit` bytes of each.

    Output beyond the limit is read and discarded, so the child never blocks on a full pipe and memory
    stays bounded; `dropped` records that something was discarded.
    """

    def __init__(self, process, limit, callback=None):
        self.callback = callback
        self.limit, self.dropped = limit, False
        self.buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
        self.selector = selectors.DefaultSelector()
        for stream in self.buffers:
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ)

    def read(self, seconds):
        """Read until both streams end (True) or `seconds` pass (False)."""
        deadline = None if seconds is None else time.monotonic() + seconds
        while self.selector.get_map():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            for key, _ in self.selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    continue
                if self.callback:
                    self.callback(key.fileobj, chunk)
                buffer = self.buffers[key.fileobj]
                room = max(self.limit - len(buffer), 0)
                buffer += chunk[:room]
                self.dropped = self.dropped or len(chunk) > room
        return True

    def close(self):
        self.selector.close()
        for stream in self.buffers:
            stream.close()

    def text(self, stream):
        return bytes(self.buffers[stream]).decode("utf-8", "replace")


def run_capture(argv, cwd, env, log=None, timeout=60, max_bytes=1_000_000, poll=False):
    """Run a probe and capture at most `max_bytes` of each stream. Logged when a log is supplied.

    `truncated` is set when output was discarded; a caller that needs complete output as evidence must treat
    that as unknown, not as the whole answer.
    """
    entry = log.record(argv, cwd, poll) if log is not None else None
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        return ProcessResult(returncode=127, stderr=str(error))
    reader = _BoundedReader(process, max_bytes)
    timed_out = incomplete = False
    try:
        started = time.monotonic()
        finished = reader.read(timeout)
        if finished:
            try:
                process.wait(timeout=None if timeout is None else max(timeout - (time.monotonic() - started), 0.001))
            except subprocess.TimeoutExpired:
                finished = False
        if not finished:
            timed_out = True
            group_gone = terminate_group(process)
            drained = reader.read(PIPE_DRAIN_SECONDS)
            incomplete = not (group_gone and drained)
    except Cancelled as cancelled:
        cancelled.cleanup_incomplete = not terminate_group(
            process, signum=CANCEL_SIGNALS.get(cancelled.exit_code, signal.SIGTERM))
        reader.close()
        raise
    if entry is not None:
        for flag, value in (("timed_out", timed_out), ("cleanup_incomplete", incomplete), ("truncated", reader.dropped)):
            if value:
                entry[flag] = True
    result = ProcessResult(returncode=124 if timed_out else process.returncode, stdout=reader.text(process.stdout),
                           stderr=reader.text(process.stderr), timed_out=timed_out, cleanup_incomplete=incomplete,
                           truncated=reader.dropped)
    reader.close()
    if log is not None:
        log.save("Probe exit: %s; timed out: %s; truncated: %s\n" % (result.returncode, timed_out, result.truncated))
    return result


def install_signal_handlers():
    def handler(signum, _frame):
        raise Cancelled(130 if signum == signal.SIGINT else 143)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
