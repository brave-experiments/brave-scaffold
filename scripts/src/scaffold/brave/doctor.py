# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only readiness checks and their aggregation."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ..common import env as env_module
from ..common import identity as identity_module
from ..common import tools as tools_module
from ..common.checks import (BLOCKER, NOT_CHECKED, PASS, UNSUPPORTED, WARNING, make_check, readiness_error)
from ..common.platforms import host_architecture, host_platform
from ..common.procs import run_capture
from ..common.results import Result, ScaffoldError, error_result, repair
from . import rbe_checks as rbe_checks_module

# Scopes delivered so far. Each scope lists the check groups it evaluates.
SCOPES = {
    "mac": ("machine", "host-mac", "mac-build", "checkout"),
    "rbe": ("machine", "rbe"),
    "shell": ("machine", "shell"),
}
EXTRA_SCOPES = {}  # populated by later stages: name -> check groups


def register_scope(name, groups):
    EXTRA_SCOPES[name] = tuple(groups)


def all_scopes():
    return {**SCOPES, **EXTRA_SCOPES}


_check = make_check


def machine_checks(ctx, scope):
    checks = [_check("scaffold-runtime", PASS, "Python %s at %s" % (sys.version.split()[0], sys.executable),
                     scope, affects=("all commands",))]
    git = shutil.which("git", path=ctx.environ.get("PATH"))
    checks.append(_check("git", PASS if git else BLOCKER, git or "git was not found on PATH.", scope,
                         affects=("source operations",), path=git))
    direnv = env_module.find_direnv(ctx.environ)
    checks.append(_check("direnv", PASS if direnv else BLOCKER,
                         direnv or "direnv is not installed; checkout commands load their environment with it.",
                         scope, affects=("checkout commands",), path=direnv,
                         repairs=[] if direnv else [repair(["open", "https://direnv.net/docs/installation.html"],
                                                           requires_user_action=True)]))
    return checks


def host_mac_checks(ctx, scope):
    ok = host_platform() == "mac" and host_architecture() == "arm64"
    checks = [_check("host-macos-arm64", PASS if ok else UNSUPPORTED,
                     "macOS arm64 host" if ok else "This host is %s/%s; macOS arm64 is the supported host." % (
                         sys.platform, host_architecture()), scope, affects=("mac build", "mac test", "mac run"))]
    if host_platform() == "mac":
        result = run_capture(["xcode-select", "-p"], os.getcwd(), ctx.environ, ctx.log, timeout=15)
        path = result.stdout.strip()
        checks.append(_check("xcode-developer-directory", PASS if result.returncode == 0 and path else BLOCKER,
                             path or "Xcode command line tools are not selected.", scope,
                             affects=("mac build",),
                             repairs=[] if path else [repair(["xcode-select", "--install"], requires_user_action=True)]))
    return checks


def checkout_checks(ctx, scope):
    """Selection, layout, environment, and local tools for one checkout."""
    def unchecked(reason, repairs=None):
        return [_check(name, NOT_CHECKED, reason, scope, affects=("checkout commands",), repairs=repairs or [])
                for name in ("checkout-layout", "environment", "local-tools")]

    try:
        identity = ctx.identity(required=True, validate=False)
    except ScaffoldError as error:
        if error.code in ("CHECKOUT_REQUIRED", "CHECKOUT_AMBIGUOUS"):
            selection = _check("checkout-selection", NOT_CHECKED, error.message, scope,
                               affects=("checkout commands",), repairs=error.repairs, **error.details)
            return [selection, *unchecked("No checkout is selected.")]
        raise
    checks = [_check("checkout-selection", PASS, "%s (selected by %s)" % (identity.core, identity.selection_source),
                     scope, core=str(identity.core))]
    worktrees = identity_module.find_linked_worktrees(identity.core, identity.src, identity.outer)
    if worktrees:
        checks.append(_check("checkout-layout", BLOCKER,
                             "Git linked worktrees are unsupported; use a separate full checkout.", scope,
                             affects=("checkout commands",), worktrees=worktrees))
        checks += [_check(name, NOT_CHECKED, "Skipped for an unsupported layout.", scope)
                   for name in ("environment", "local-tools")]
        return checks
    checks.append(_check("checkout-layout", PASS, "Full checkout", scope))
    checks.append(_environment_check(ctx, identity, scope))
    _, tool_checks = tools_module.inspect_toolchain(identity, ctx.log)
    for check in tool_checks:
        check.scopes = (scope,)
    ready = all(check.status == PASS or not check.required for check in tool_checks)
    checks.append(_check("local-tools", PASS if ready else BLOCKER,
                         "Checkout-local tools are ready." if ready else "Checkout-local tools are not ready.",
                         scope, affects=("package commands",), checks=[c.to_dict() for c in tool_checks],
                         repairs=[] if ready else [repair(tools_module.tools_setup_command(identity),
                                                          note="Explicit repair; run only when authorized.")]))
    return checks


def _environment_check(ctx, identity, scope):
    try:
        env_module.load_environment(identity, ctx.environ, ctx.log)
    except ScaffoldError as error:
        return _check("environment", BLOCKER, error.message, scope, affects=("checkout commands",),
                      repairs=error.repairs, code=error.code, **error.details)
    return _check("environment", PASS, "Approved environment loads and selects this checkout.", scope,
                  affects=("checkout commands",))


def shell_checks(ctx, scope):
    checks = []
    shell = ctx.environ.get("SHELL", "")
    name = Path(shell).name
    if name not in ("zsh", "bash"):
        checks.append(_check("shell-hook", NOT_CHECKED, "Shell %r is not inspected." % (shell or "unset"), scope,
                             required=False))
    else:
        probe = ('print -r -- "${precmd_functions[(Ie)_direnv_hook]}"' if name == "zsh"
                 else 'case "$PROMPT_COMMAND" in *_direnv_hook*) echo 1;; *) echo 0;; esac')
        result = run_capture([shell, "-ic", probe], os.getcwd(), ctx.environ, ctx.log, timeout=30)
        hooked = result.returncode == 0 and result.stdout.strip().splitlines()[-1:] not in ([], ["0"])
        checks.append(_check("shell-hook", PASS if hooked else WARNING,
                             "direnv hook is active in interactive %s." % name if hooked else
                             "The direnv hook is not active in interactive %s. Direct commands do not need it." % name,
                             scope, required=False, affects=("automatic activation",)))
    launcher = ctx.scaffold_root / "scripts" / "bdev"
    found = shutil.which("bdev", path=ctx.environ.get("PATH"))
    same = found and os.path.realpath(found) == os.path.realpath(launcher)
    checks.append(_check("bdev-on-path", PASS if same else WARNING,
                         "bdev resolves to this installation." if same else
                         "bdev is not on PATH (or resolves elsewhere). Invoke %s directly." % launcher,
                         scope, required=False, found=found))
    return checks


GROUP_FUNCTIONS = {"machine": machine_checks, "host-mac": host_mac_checks, "checkout": checkout_checks,
                   "shell": shell_checks, "mac-build": rbe_checks_module.mac_build_checks,
                   "rbe": rbe_checks_module.rbe_checks}


def register_group(name, function):
    GROUP_FUNCTIONS[name] = function


def run_doctor(ctx):
    scopes_map = all_scopes()
    requested = ctx.parsed.positionals[0].lower() if ctx.parsed.positionals else None
    if requested is not None and requested not in scopes_map:
        deferred = requested in ("ios", "android-studio", "emulator", "agent", "agents", "skills")
        raise ScaffoldError(
            "UNSUPPORTED_CAPABILITY" if deferred else "INVALID_INPUT",
            "Doctor scope %r is %s." % (requested, "not available in this release" if deferred else "unknown"),
            details={"scopes": sorted(scopes_map)})
    scopes = [requested] if requested else list(scopes_map)
    groups = {}
    for scope in scopes:
        for group in scopes_map[scope]:
            groups.setdefault(group, []).append(scope)
    checks = []
    for group, group_scopes in groups.items():
        for check in GROUP_FUNCTIONS[group](ctx, group_scopes[0]):
            check.scopes = tuple(group_scopes)
            checks.append(check)
    result = Result(command="doctor", data={"scopes": scopes})
    result.checks = [check.to_dict() for check in checks]
    for check in checks:
        if check.status != PASS and not (check.required and check.status != WARNING):
            result.add_warning("CHECK_" + check.status.upper(), "%s: %s" % (check.name, check.summary))
    lines = ["Doctor scopes: %s" % ", ".join(scopes)]
    for check in checks:
        lines.append("  %-11s %s%s: %s" % (check.status.upper(), check.name, "" if check.required else " (optional)",
                                             check.summary))
    result.text = "\n".join(lines)
    error = readiness_error(checks)
    if error is not None:
        failed = error_result("doctor", error, ctx.selected.to_context() if ctx.selected else None)
        failed.checks, failed.data, failed.warnings, failed.text = result.checks, result.data, result.warnings, result.text
        return failed
    return result


def _register_signing():
    from .signing_checks import signing_checks
    register_group("signing", signing_checks)
    register_scope("signing", ("machine", "signing"))


_register_signing()
