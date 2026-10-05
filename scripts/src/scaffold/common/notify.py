# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Desktop completion notifications: policy, classification, message text, and delivery."""

from __future__ import annotations

import os
import subprocess
import sys

from .procs import format_duration

POLICIES = ("always", "major", "never")
DEFAULT_POLICY = "major"
DELIVERIES = ("desktop", "bell", "both")
DEFAULT_DELIVERY = "desktop"
TERMINAL_DEVICE = "/dev/tty"
BACKEND_VARIABLE = "BDEV_NOTIFY_BACKEND"

# Operations that `major` covers. Everything else that performs work is notified only by `always`.
MAJOR_COMMANDS = frozenset({
    "sync", "build", "build-run", "sync-build", "sync-build-run", "test", "run", "deploy",
    "setup", "env init", "tools setup", "android setup", "patches update", "clean"})
# Commands that print or export without performing an operation.
SILENT_COMMANDS = frozenset({"env export"})

MAX_TEXT = 200


def normalize_policy(value):
    """A policy name, or None when the value is not one."""
    if isinstance(value, str) and value.lower() in POLICIES:
        return value.lower()
    return None


def effective_policy(parsed, config):
    """An explicit CLI policy wins over configuration; the default applies when neither is set."""
    cli = parsed.get("notify")
    if cli is not None:
        return cli
    return getattr(config, "notification_policy", None) or DEFAULT_POLICY


def effective_delivery(config):
    return getattr(config, "notification_delivery", None) or DEFAULT_DELIVERY


def is_operation(command, parsed):
    """False for previews and pure exports, which stay silent under every policy."""
    if command in SILENT_COMMANDS or parsed.get("plan"):
        return False
    if command == "clean" and not parsed.get("execute"):
        return False
    return True


def should_notify(policy, command, parsed):
    if policy == "never" or not is_operation(command, parsed):
        return False
    if policy == "always":
        return True
    return command in MAJOR_COMMANDS


def _short(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 3] + "..."


def compose(command, result, elapsed, log_path=None):
    """(title, body). Only the command name, checkout name, status, exit code, and error code appear;
    error messages and child output can carry paths or arguments and are left to the log."""
    context = result.context or {}
    checkout = context.get("alias") or os.path.basename(str(context.get("checkout") or "")) or None
    if result.status == "ok" and result.exit_code == 0:
        outcome = "succeeded"
    elif result.status == "cancelled":
        outcome = "cancelled"
    else:
        outcome = "failed"
    title = "bdev %s %s" % (command, outcome)
    error = result.error or {}
    if outcome == "failed":
        detail = "exit %d" % result.exit_code
        if error.get("code"):
            detail = "%s (%s)" % (error["code"], detail)
    elif outcome == "cancelled":
        detail = "interrupted (exit %d)" % result.exit_code
    else:
        detail = "exit 0"
    lines = []
    if checkout:
        lines.append("Checkout: %s" % checkout)
    lines.append("Elapsed: %s" % format_duration(elapsed))
    lines.append(_short(detail))
    if log_path:
        lines.append("Log: %s" % log_path)
    return title, "\n".join(lines)


class MacNotifier:
    """Notification Center through the system `osascript`; no service, no Core involvement.

    Text is passed as script arguments, never spliced into script source.
    """

    SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")

    def send(self, title, body):
        argv = ["osascript"]
        for line in self.SCRIPT:
            argv += ["-e", line]
        subprocess.run(argv + [body, title], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=True)


class NoTerminal(Exception):
    """The process has no controlling terminal to ring."""


class TerminalBell:
    """A terminal bell (BEL) written to the controlling terminal, never to stdout or stderr.

    Whether the bell is heard, shown as a visual flash, or ignored depends on the terminal's settings.
    """

    def ring(self):
        try:
            fd = os.open(TERMINAL_DEVICE, os.O_WRONLY | os.O_NOCTTY)
        except OSError as error:
            raise NoTerminal(str(error)) from error
        try:
            os.write(fd, b"\a")
        finally:
            os.close(fd)


def default_notifier(environ):
    if environ.get(BACKEND_VARIABLE) == "none" or sys.platform != "darwin":
        return None
    return MacNotifier()


def default_bell(environ):
    return None if environ.get(BACKEND_VARIABLE) == "none" else TerminalBell()


def deliver_bell(bell):
    """Ring once. Returns an error text on failure; a missing terminal is skipped, not an error."""
    try:
        bell.ring()
    except NoTerminal:
        return None
    except Exception as error:  # noqa: BLE001 - delivery must never change the command's outcome
        return "%s: %s" % (type(error).__name__, error)
    return None


def deliver(notifier, title, body):
    """Send one notification. Returns an error text on failure; never raises."""
    try:
        notifier.send(title, body)
    except Exception as error:  # noqa: BLE001 - delivery must never change the command's outcome
        return "%s: %s" % (type(error).__name__, error)
    return None
