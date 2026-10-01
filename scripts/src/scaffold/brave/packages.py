# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Package commands: the one place that turns arguments and an execution context into a running child."""

from __future__ import annotations

import contextlib
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

from ..common import tools as tools_module
from ..common.procs import run_streaming


# Routine messages printed by Core's Java bytecode rewriter.
BYTECODE_DETAIL = re.compile(
    r"^(?:redirecting constructor from |redirecting ownership for |redirecting type in method "
    r"|changing owner for |change superclass of |use invoke virtual for call to method "
    r"|make .+ (?:public|private) in .+$|make Class .+ non final$"
    r"|delete .+ from .+$|add .+ annotation to .+ in .+$)")


def bytecode_detail(text):
    return bool(BYTECODE_DETAIL.match(text.rstrip("\r\n")))


@contextlib.contextmanager
def local_shims(toolchain):
    """A private directory with a `pnpm` command bound to the checkout's payload."""
    if toolchain.manager != "pnpm":
        yield None
        return
    directory = tempfile.mkdtemp(prefix="scaffold-shims-")
    try:
        shim = Path(directory) / "pnpm"
        shim.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(str(toolchain.node)),
                                                             shlex.quote(str(toolchain.manager_entry))))
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def run(ctx, execution, arguments, extra_env=None):
    """Run a package command in Core with the checkout-local tools; returns (argv, exit code).

    `arguments` are forwarded exactly (older npm gets its `--` separator), the child sees the checkout's
    environment with the tools first on PATH, and `extra_env` adjusts only that child's environment (a value
    of None removes the variable). Callers format the result and decide what a nonzero exit means.
    """
    argv = tools_module.package_argv(execution.toolchain, arguments)
    with local_shims(execution.toolchain) as shims:
        env = tools_module.child_environment(execution.environ, execution.toolchain, shims)
        for name, value in (extra_env or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return argv, run_streaming(argv, str(execution.identity.core), env, ctx.log, json_mode=ctx.json_mode,
                                   preserve_stdout=ctx.command == "bpm",
                                   verbose_output=bytecode_detail if ctx.command != "bpm" and
                                   list(arguments[:2]) == ["run", "build"] else None,
                                   display_argv=[execution.toolchain.manager, *argv[2:]])
