# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Git SSH signing protocol helper that bounds a 1Password signing attempt.

Stdout carries only the signer's output; diagnostics go to stderr. A signing
failure is never converted into an unsigned result.
"""

import os
import plistlib
import signal
import subprocess
import sys
import time

SIGNER = "/Applications/1Password.app/Contents/MacOS/op-ssh-sign"
APP = "/Applications/1Password.app"
TIMEOUT_SECONDS = 15
STARTUP_SECONDS = 2
SOCKET_ERROR = b"1Password: Could not connect to socket. Is the agent running?"


def run(command, deadline):
    """Run with Git's arguments and stdin; stop the whole process group on timeout or cancel."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=remaining)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def session_is_unlocked(registry, uid):
    if not isinstance(registry, dict) or registry.get("IOConsoleLocked") is not False:
        return False
    users = registry.get("IOConsoleUsers")
    if not isinstance(users, list):
        return False
    return any(isinstance(user, dict)
               and user.get("kCGSSessionUserIDKey") == uid
               and user.get("kCGSSessionOnConsoleKey") is True
               and user.get("kCGSessionLoginDoneKey") is True
               for user in users)


def can_open_app(deadline):
    """Only raise the app in an unlocked local console session, never over SSH."""
    if sys.platform != "darwin" or os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    try:
        result = run(["/usr/sbin/ioreg", "-a", "-n", "Root", "-d", "1"],
                     min(deadline, time.monotonic() + 2))
        return result.returncode == 0 and session_is_unlocked(plistlib.loads(result.stdout), os.getuid())
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.TimeoutExpired):
        return False


def sign(arguments, deadline):
    command = [SIGNER, *arguments]
    result = run(command, deadline)
    if (result.returncode != 0 and SOCKET_ERROR in result.stderr
            and "sign" in arguments and can_open_app(deadline)):
        print("1Password signing: opening the app and retrying once (15-second total limit).",
              file=sys.stderr)
        opened = run(["/usr/bin/open", "-a", APP], deadline)
        if opened.returncode != 0:
            sys.stderr.buffer.write(opened.stderr)
            return result
        time.sleep(min(STARTUP_SECONDS, max(0, deadline - time.monotonic())))
        if can_open_app(deadline):
            result = run(command, deadline)
    return result


def cancelled(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGHUP, cancelled)
    try:
        result = sign(argv, time.monotonic() + TIMEOUT_SECONDS)
        sys.stderr.buffer.write(result.stderr)
        if result.returncode == 0:
            sys.stdout.buffer.write(result.stdout)
            return 0
        print("1Password signing: failed; no further retry. Git signing remains required.",
              file=sys.stderr)
        return result.returncode if result.returncode > 0 else 1
    except subprocess.TimeoutExpired:
        print("1Password signing: timed out after %d seconds; authentication may be unanswered. "
              "The signing process was stopped; no further retry." % TIMEOUT_SECONDS, file=sys.stderr)
        return 124
    except OSError as error:
        print("1Password signing: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("1Password signing: cancelled.", file=sys.stderr)
        return 130
