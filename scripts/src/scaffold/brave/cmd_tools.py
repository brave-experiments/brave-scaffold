# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Strict package-manager execution, direct Python, and explicit tool repair."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

from ..common import env as env_module
from ..common import tools as tools_module
from ..common.procs import run_streaming
from ..common.results import Result, ScaffoldError, repair
from .records import track


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


def mark_child_failure(result, name, argv, cwd, code):
    """Turn a result into the CHILD_FAILED error for a child that exited nonzero."""
    error = ScaffoldError("CHILD_FAILED", "%s exited with status %d." % (name, code),
                          details={"argv": argv, "cwd": str(cwd)}, child_exit_code=code)
    result.status, result.exit_code = "error", error.exit_code
    result.error = {"code": error.code, "message": error.message, "details": error.details, "repairs": []}


def run_package(ctx, identity, arguments, command):
    """Run a package command in Core with checkout-local tools; return a Result."""
    loaded = env_module.load_environment(identity, ctx.environ, ctx.log)
    toolchain, checks = tools_module.require_toolchain(identity, ctx.log)
    argv = tools_module.package_argv(toolchain, arguments)
    with local_shims(toolchain) as shims:
        child_env = tools_module.child_environment(loaded, toolchain, shims)
        code = run_streaming(argv, str(identity.core), child_env, ctx.log, json_mode=ctx.json_mode)
    data = {"argv": argv, "requested_arguments": list(arguments), "cwd": str(identity.core),
            "tools": toolchain.describe()}
    result = Result(command=command, data=data, child_exit_code=code,
                    checks=[check.to_dict() for check in checks])
    if code != 0:
        mark_child_failure(result, toolchain.manager, argv, identity.core, code)
    return result


def bpm_run(ctx):
    identity = ctx.identity()
    return run_package(ctx, identity, ctx.parsed.forwarded, "bpm")


def vpython3(ctx):
    identity = ctx.identity()
    base = Path(ctx.cwd)
    requested = ctx.parsed.get("cwd")
    execution_cwd = base
    if requested:
        execution_cwd = Path(requested)
        if not execution_cwd.is_absolute():
            execution_cwd = base / execution_cwd
        execution_cwd = Path(os.path.normpath(execution_cwd))
        if not execution_cwd.is_dir():
            raise ScaffoldError("INVALID_INPUT", "--cwd %s is not a directory." % requested,
                                details={"resolved": str(execution_cwd)})
    loaded = env_module.load_environment(identity, ctx.environ, ctx.log)
    interpreter = Path(loaded["VPYTHON3"])
    if not os.access(interpreter, os.X_OK):
        raise ScaffoldError("LOCAL_TOOL_MISSING", "Checkout-local vpython3 is not executable: %s" % interpreter,
                            repairs=[repair(tools_module.tools_setup_command(identity))])
    argv = [str(interpreter), *ctx.parsed.forwarded]
    code = run_streaming(argv, str(execution_cwd), loaded, ctx.log, json_mode=ctx.json_mode)
    result = Result(command="vpython3", child_exit_code=code,
                    data={"argv": argv, "interpreter": str(interpreter), "cwd": str(execution_cwd)})
    if code != 0:
        mark_child_failure(result, "vpython3", argv, execution_cwd, code)
    return result


def tools_setup(ctx):
    """Explicit provisioning of checkout-local payloads using the checkout's own installer."""
    identity = ctx.identity()
    loaded = env_module.load_environment(identity, ctx.environ, ctx.log)
    layout = tools_module.node_layout(identity.core)
    installer = identity.core / "tools" / "cr" / "tarball_installer.py"
    if not installer.is_file():
        raise ScaffoldError(
            "DEPENDENCY_INCOMPATIBLE",
            "This checkout has no supported payload installer (%s); tool repair is not available for it." % installer,
            details={"checkout": str(identity.core)})
    escaped = tools_module.payload_escapes(identity)
    if escaped:
        raise ScaffoldError(
            "OWNERSHIP_CONFLICT",
            "third_party/node resolves outside the checkout (%s), so repairing it would write there; nothing was "
            "changed." % escaped, details={"payload": str(identity.core / "third_party" / "node"), "resolves_to": escaped})
    entries = [layout["node_entry_key"], layout["pnpm_entry_key"]]
    with track(ctx, "tools setup", identity, {"installer": str(installer), "entries": entries}, validated=True) as op:
        ran = []
        for entry in entries:
            argv = [str(loaded["VPYTHON3"]), str(installer), entry]
            code = run_streaming(argv, str(identity.core), loaded, ctx.log, json_mode=ctx.json_mode)
            ran.append({"argv": argv, "exit": code})
            op.step("installer", entry=entry, exit=code)
            if code != 0:
                raise ScaffoldError("CHILD_FAILED", "The payload installer failed for %s." % entry,
                                    details={"argv": argv}, child_exit_code=code)
        after, after_checks = tools_module.inspect_toolchain(identity, ctx.log)
        result = Result(command="tools setup", data={"installer_runs": ran, "ready": after is not None,
                                                      "tools": after.describe() if after else None},
                        checks=[check.to_dict() for check in after_checks])
        result.text = "Ran the checkout's payload installer for %d entries.\nTools ready: %s" % (
            len(entries), "yes" if after else "no; see checks")
        if after is None:
            raise ScaffoldError("LOCAL_TOOL_MISSING", "Tools are still not ready after repair.",
                                details={"checks": [c.to_dict() for c in after_checks if c.status != "pass"]})
        return op.complete(result)
