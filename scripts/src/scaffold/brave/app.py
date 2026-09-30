# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared command context and the result-producing runner."""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass

from ..common import config as config_module
from ..common import identity as identity_module
from ..common.procs import CommandLog
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
    prepared: bool = False

    def load_config(self, may_create=False):
        explicit = self.parsed.get("config")
        path = explicit
        if path and not os.path.isabs(os.path.expanduser(path)):
            path = os.path.join(self.cwd, path)
        self.config = config_module.load_config(path, explicit=bool(explicit) and not may_create)
        self.log.enabled = self.config.commands_logging
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
                may_create_config=False):
    """Run a handler and always emit exactly one result; return the exit code."""
    json_mode = parsed.json_mode
    log = CommandLog(stream=stderr)
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
    emit(result, json_mode, stdout=stdout, stderr=stderr)
    return result.exit_code

