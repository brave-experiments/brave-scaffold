# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The `bpm` command line: run the checkout's package manager directly."""

from __future__ import annotations

import sys

from ..common.cli import CommandSpec, Parsed, detect_json_leading, help_requested, parse_leading, render_help
from ..common.results import ScaffoldError
from . import cmd_tools
from ..common.procs import install_signal_handlers
from .app import run_command

SPEC = CommandSpec(
    "bpm", "Run the checkout's declared package manager with your arguments.", cmd_tools.bpm_run, forward=True, leading_only=True,
    side_effects="Whatever the package command does. Runs in the Core directory with checkout-local Node "
                 "and package manager; never falls back to global tools.",
    notes="Scaffold options are accepted only before the first package argument. From that argument on, "
          "everything (including --json and --checkout) goes to the package manager. "
          "Use 'bpm -- --help' to ask the package manager for help.",
    examples=("bpm run build", "bpm --checkout main run test -- --filter Example", "bpm -- --version"))


def main(argv, stdout=None, stderr=None, notifier=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    install_signal_handlers()
    if help_requested(SPEC, list(argv)):
        stdout.write(render_help(SPEC, prefix="bpm"))
        return 0
    try:
        parsed = parse_leading(SPEC, list(argv))
    except ScaffoldError as error:
        failure = Parsed(values={"json": detect_json_leading(SPEC, list(argv))})

        def raise_error(_context):
            raise error
        return run_command("bpm", failure, raise_error, needs_config=False, stdout=stdout, stderr=stderr, notify=False)
    return run_command("bpm", parsed, cmd_tools.bpm_run, stdout=stdout, stderr=stderr, notifier=notifier)
