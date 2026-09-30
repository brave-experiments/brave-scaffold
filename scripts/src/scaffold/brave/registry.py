# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Public command definitions. Help, parsing, and dispatch all read this table."""

from __future__ import annotations

from ..common.cli import CommandSpec, Opt, Positional
from . import clean, cmd_setup, cmd_tools, doctor

WITH_PYTHONPATH = Opt("--with-pythonpath", "with_pythonpath", takes_value=False,
                      help="Also export PYTHONPATH for Core's script directory.")
CWD = Opt("--cwd", "cwd", metavar="DIRECTORY",
          help="Run in this directory (relative to your current directory) instead of the current one.")

# Commands that take a group word first ("checkout add"). The value is the set of subcommands.
GROUPS = {"checkout": ("add", "list"), "env": ("init", "export", "check"), "tools": ("setup",)}


def build_registry():
    specs = [
        CommandSpec("context", "Show the resolved checkout, environment mapping, configuration, and tool state.",
                    cmd_setup.context, examples=("bdev context", "bdev context --checkout main --json")),
        CommandSpec("capabilities", "List supported, limited, unverified, and unsupported combinations.",
                    cmd_setup.capabilities, examples=("bdev capabilities --json",)),
        CommandSpec("doctor", "Check readiness for a scope without repairing anything.", doctor.run_doctor,
                    positionals=(Positional("scope", help="mac or shell; omit for all delivered scopes."),),
                    examples=("bdev doctor", "bdev doctor mac --checkout main"),
                    notes="Checking a selected checkout evaluates its approved environment file."),
        CommandSpec("setup", "Inspect prerequisites and prepare scaffold-owned configuration.", cmd_setup.setup, creates_config=True,
                    side_effects="Creates brave-scaffold.toml beside the scaffold when it is missing. "
                                 "Nothing inside Brave Core changes.",
                    examples=("bdev setup",)),
        CommandSpec("checkout add", "Register an existing checkout under a name.", cmd_setup.checkout_add, creates_config=True,
                    positionals=(Positional("name", True, help="Alias for the checkout."),
                                 Positional("path", True, help="Core, Chromium src, or outer checkout path.")),
                    side_effects="Edits brave-scaffold.toml. Nothing inside the checkout changes.",
                    examples=("bdev checkout add main /work/browser/_bad_scm/workspace/src/brave",)),
        CommandSpec("checkout list", "List registered checkouts, environment state, and invalid registrations.",
                    cmd_setup.checkout_list, examples=("bdev checkout list",)),
        CommandSpec("env init", "Generate the scaffold-owned environment for a checkout and print the approval command.",
                    cmd_setup.env_init,
                    side_effects="Writes <environment-dir>/.envrc and the checkout record in brave-scaffold.toml. "
                                 "Never approves the file and never writes inside Brave Core.",
                    examples=("bdev env init --checkout main",)),
        CommandSpec("env export", "Print derived checkout exports for a generated .envrc (no direnv, no tool changes).",
                    cmd_setup.env_export, options=(WITH_PYTHONPATH,),
                    examples=("bdev env export --checkout main --format bash",)),
        CommandSpec("env check", "Compare the loaded environment against the checkout identity and tools.",
                    cmd_setup.env_check, options=(WITH_PYTHONPATH,),
                    notes="Evaluates the approved environment file, which is user-reviewed code.",
                    examples=("bdev env check --checkout main",)),
        CommandSpec("shell", "Start a child shell in Core using the approved environment.", cmd_setup.shell,
                    notes="Exiting the shell leaves your original environment unchanged.",
                    examples=("bdev shell --checkout main",)),
        CommandSpec("tools setup", "Explicitly provision or repair checkout-local Node and package-manager payloads.",
                    cmd_tools.tools_setup,
                    side_effects="Downloads and deploys the checkout's pinned payloads inside the checkout's "
                                 "third_party/node directory using the checkout's own installer.",
                    examples=("bdev tools setup --checkout main",)),
        CommandSpec("vpython3", "Run the checkout-local vpython3 with your arguments.", cmd_tools.vpython3,
                    options=(CWD,), forward=True, leading_only=True,
                    side_effects="Whatever the Python program does. Runs in your current directory unless --cwd is given.",
                    notes="Scaffold options must come before the first Python argument. Use -- to be explicit.",
                    examples=("bdev vpython3 -- tools/example.py --flag", "bdev vpython3 --cwd out -- ../script.py")),
        clean.SPEC,
    ]
    registry = {}
    for spec in specs:
        registry[spec.name] = spec
        for alias in spec.aliases:
            registry[alias] = spec
    return registry


REGISTRY = build_registry()
