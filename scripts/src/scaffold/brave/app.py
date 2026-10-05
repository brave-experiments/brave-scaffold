# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared command context and the result-producing runner."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass

from ..common import config as config_module
from ..common import identity as identity_module
from ..common import notify as notify_module
from ..common.procs import CommandLog, format_duration
from ..common.results import (Cancelled, EXIT_INTERNAL, Result, ScaffoldError, emit, error_result)


@dataclass
class Context:
    command: str
    parsed: object
    environ: dict
    cwd: str
    json_mode: bool
    config: object = None
    log: CommandLog = None
    scaffold_root: object = None
    selected: object = None

    def load_config(self, may_create=False):
        explicit = self.parsed.get("config")
        path = explicit
        if path and not os.path.isabs(os.path.expanduser(path)):
            path = os.path.join(self.cwd, path)
        self.config = config_module.load_config(path, explicit=bool(explicit) and not may_create)
        self.log.verbosity = self.parsed.get("verbosity", self.config.verbosity)
        if not self.log.path and self.command not in ("env export", "cd") and not self.parsed.get("plan"):
            self.log.open(self.config.directory)
        return self.config

    @property
    def state_root(self):
        """Operation records and output state live beside the active configuration file."""
        return self.config.directory

    def identity(self, required=True, validate=True):
        """Select the checkout: explicit selector, otherwise the caller's cwd."""
        if self.selected is not None:
            return self.selected
        selected = identity_module.select_checkout(
            self.config, self.parsed.get("checkout"), self.cwd, required=required)
        if selected is not None and validate:
            identity_module.validate_layout(selected)
        self.selected = selected
        return selected


def run_command(command, parsed, handler, argv_environ=None, needs_config=True, cwd=None, stdout=None, stderr=None,
                may_create_config=False, notify=True, notifier=None):
    """Run a handler and always emit exactly one result; return the exit code.

    One completion notification is sent per call after the result is emitted, so combined commands
    (whose phases run inside one handler) notify once with the final outcome. `notify=False` marks
    calls that performed no operation, such as a rejected command line.
    """
    json_mode = parsed.json_mode
    started = time.monotonic()
    log = CommandLog(stream=stderr, verbosity=parsed.get("verbosity", "normal"))
    context = Context(command=command, parsed=parsed, environ=dict(argv_environ or os.environ),
                      cwd=cwd or os.getcwd(), json_mode=json_mode, log=log,
                      scaffold_root=config_module.scaffold_root())
    result = None
    try:
        if needs_config:
            context.load_config(may_create_config)
        result = handler(context)
    except ScaffoldError as error:
        result = error_result(command, error, context.selected.to_context() if context.selected else None)
    except Cancelled as cancelled:
        result = Result(command=command, status="cancelled", exit_code=cancelled.exit_code,
                        operation_id=cancelled.operation_id)
        result.error = {"code": "CANCELLED", "message": "The command was interrupted.", "details": {}, "repairs": []}
        if cancelled.completed_phases:
            result.error["details"]["completed_phases"] = cancelled.completed_phases
        if cancelled.cleanup_incomplete:
            result.error["details"]["cleanup_incomplete"] = True
            result.add_warning("CLEANUP_INCOMPLETE", "Some processes started by this command may still be running; "
                               "inspect them before retrying.")
        if context.selected:
            result.context = context.selected.to_context()
    except Exception as error:  # noqa: BLE001 - last-resort boundary for the one-document guarantee
        result = Result(command=command, status="error", exit_code=EXIT_INTERNAL)
        result.error = {"code": "INTERNAL_ERROR", "message": "Unexpected failure: %s" % error,
                        "details": {"exception": type(error).__name__}, "repairs": []}
        if os.environ.get("BDEV_TRACEBACK"):
            traceback.print_exc(file=stderr or sys.stderr)
    if context.selected and result.context.get("checkout") is None:
        result.context = context.selected.to_context()
    if context.log.records and not result.logs:
        result.logs = [{"kind": "command", **record} for record in context.log.records]
    elapsed = time.monotonic() - started
    try:
        log.report_timings(elapsed)
        if log.path:
            log.save("Result: %s; exit %d; elapsed %.2fs\n" % (result.status, result.exit_code, elapsed))
            safe = result.redacted()
            if safe.error or safe.warnings:
                log.save(json.dumps({"error": safe.error, "warnings": safe.warnings}) + "\n")
        log.close()
    except OSError as error:
        result.add_warning("LOG_WRITE_FAILED", "The diagnostic log could not be completed: %s" % error)
    emit(result, json_mode, stdout=stdout, stderr=stderr)
    if log.path:
        log.message("⏱️  Elapsed: %s\nLog: %s" % (format_duration(elapsed), log.path))
    if notify and (context.config is not None or not needs_config):
        _notify(context, command, result, elapsed, notifier)
    return result.exit_code


def _notify(context, command, result, elapsed, notifier):
    try:
        config = context.config
        if config is None:
            try:
                explicit = context.parsed.get("config")
                if explicit and not os.path.isabs(os.path.expanduser(explicit)):
                    explicit = os.path.join(context.cwd, explicit)
                config = config_module.load_config(explicit)
            except ScaffoldError:
                config = None
        policy = notify_module.effective_policy(context.parsed, config)
        if not notify_module.should_notify(policy, command, context.parsed):
            return
        if notifier is None:
            notifier = notify_module.default_notifier(context.environ)
        if notifier is None:
            return
        title, body = notify_module.compose(command, result.redacted(), elapsed, context.log.path)
        failure = notify_module.deliver(notifier, title, body)
        if failure:
            context.log.message("Notification not delivered: %s" % failure)
    except Exception:  # noqa: BLE001 - notification problems never change the command's outcome
        pass
