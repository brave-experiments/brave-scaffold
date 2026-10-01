# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only readiness checks and their aggregation."""

from __future__ import annotations

import os
import shutil
import shlex
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from ..common import env as env_module
from ..common import identity as identity_module
from ..common import tools as tools_module
from ..common.checks import (BLOCKER, MARKER_LEGEND, MARKERS, NOT_CHECKED, PASS, UNSUPPORTED, WARNING,
                             CheckResult, display_label, make_check, readiness_error)
from ..common.platforms import host_architecture, host_platform
from ..common.procs import run_capture
from ..common.results import Result, ScaffoldError, error_result, repair
from . import android_checks, execution as execution_module, signing_checks
from . import rbe_checks as rbe_checks_module

# Each scope lists the check groups it evaluates.
SCOPES = {
    "mac": ("machine", "host-mac", "mac-build", "checkout"),
    "rbe": ("machine", "rbe"),
    "shell": ("machine", "shell"),
    "signing": ("machine", "signing"),
    "android": ("machine", "host-mac", "android-machine", "checkout", "android-build", "android-support"),
}


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


@dataclass
class CheckoutState:
    """What one doctor run established about the selected checkout, so every group judges the same thing."""

    identity: object = None
    execution: object = None  # the approved environment, loaded and validated
    selection_error: object = None
    worktrees: tuple = ()
    environment_error: object = None


def checkout_state(ctx):
    try:
        identity = ctx.identity(required=True, validate=False)
    except ScaffoldError as error:
        if error.code in ("CHECKOUT_REQUIRED", "CHECKOUT_AMBIGUOUS"):
            return CheckoutState(selection_error=error)
        raise
    worktrees = identity_module.find_linked_worktrees(identity.core, identity.src, identity.outer)
    if worktrees:
        return CheckoutState(identity, worktrees=tuple(worktrees))
    try:
        return CheckoutState(identity, execution_module.load(ctx, identity))
    except ScaffoldError as error:
        return CheckoutState(identity, environment_error=error)


def checkout_checks(ctx, scope, state):
    """Selection, layout, environment, and local tools for one checkout."""
    def unchecked(reason, repairs=None, names=("checkout-layout", "environment", "local-tools")):
        return [_check(name, NOT_CHECKED, reason, scope, affects=("checkout commands",), repairs=repairs or [])
                for name in names]

    if state.selection_error is not None:
        error = state.selection_error
        selection = _check("checkout-selection", NOT_CHECKED, error.message, scope,
                           affects=("checkout commands",), repairs=error.repairs, **error.details)
        return [selection, *unchecked("No checkout is selected.")]
    identity = state.identity
    checks = [_check("checkout-selection", PASS, "%s (selected by %s)" % (identity.core, identity.selection_source),
                     scope, core=str(identity.core))]
    if state.worktrees:
        checks.append(_check("checkout-layout", BLOCKER,
                             "Git linked worktrees are unsupported; use a separate full checkout.", scope,
                             affects=("checkout commands",), worktrees=list(state.worktrees)))
        return checks + unchecked("Skipped for an unsupported layout.", names=("environment", "local-tools"))
    checks.append(_check("checkout-layout", PASS, "Full checkout", scope))
    checks.append(_environment_check(state, scope))
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


def _environment_check(state, scope):
    error = state.environment_error
    if error is not None:
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


GROUP_FUNCTIONS = {"machine": machine_checks, "host-mac": host_mac_checks, "shell": shell_checks,
                   "mac-build": rbe_checks_module.mac_build_checks, "rbe": rbe_checks_module.rbe_checks,
                   "signing": signing_checks.signing_checks, "android-machine": android_checks.machine_checks,
                   "android-build": android_checks.build_checks, "android-support": android_checks.support_checks}


# These describe the machine, the shell, or Git, not the checkout, so the caller's environment is the right one.
# Every other group is judged in the environment a command on the selected checkout would run in.
MACHINE_GROUPS = ("machine", "shell", "signing")


def group_checks(ctx, group, scope, state):
    """One group's checks, in the caller's environment or the selected checkout's approved one."""
    if group == "checkout":
        return checkout_checks(ctx, scope, state)
    function = GROUP_FUNCTIONS[group]
    if group in MACHINE_GROUPS or state.identity is None and state.selection_error is not None:
        return function(ctx, scope)
    if state.execution is None:
        reason = ("The checkout's approved environment could not be loaded, so this would be judged in the caller's "
                  "environment instead of the one commands run in." if state.environment_error is not None
                  else "Skipped for an unsupported layout.")
        return [_check("readiness:" + group, NOT_CHECKED, reason, scope, affects=("checkout commands",))]
    return function(state.execution.context(ctx), scope)


def merge_checks(checks):
    """Report shared checks once, retaining the strongest result and every affected scope."""
    merged = {}
    rank = {PASS: 0, WARNING: 1, NOT_CHECKED: 2, UNSUPPORTED: 3, BLOCKER: 4}
    for check in checks:
        previous = merged.get(check.name)
        if previous is None:
            merged[check.name] = check
            continue
        scopes = tuple(dict.fromkeys((*previous.scopes, *check.scopes)))
        affects = tuple(dict.fromkeys((*previous.affects, *check.affects)))
        repairs = previous.repairs + [step for step in check.repairs if step not in previous.repairs]
        required = previous.required or check.required
        chosen = check if rank[check.status] > rank[previous.status] else previous
        chosen.scopes, chosen.affects, chosen.repairs, chosen.required = scopes, affects, repairs, required
        merged[check.name] = chosen
    return list(merged.values())


def render_text(scopes, checks, *, hidden=(), heading="🩺 Brave setup doctor", show_scopes=True):
    """Group results and show each problem and suggested repair once."""
    selection = next((check for check in checks if check.name == "checkout-selection"), None)
    missing = selection is not None and selection.status == NOT_CHECKED
    lines = [heading]
    if show_scopes:
        lines.append("Scopes: %s" % ", ".join(scopes))
    sections = {}
    for check in checks:
        if check.name in hidden:
            continue
        if missing and check.status == NOT_CHECKED and (
                check.name in ("checkout-selection", "checkout-layout", "environment", "local-tools", "services-key")
                or check.name.startswith("android-")
                or check.name.startswith("rbe-") and check.name != "rbe-reachability"):
            continue
        if check.name.startswith("rbe-"):
            section = "RBE"
        elif check.name in ("checkout-selection", "checkout-layout", "environment", "local-tools", "services-key"):
            section = "Checkout"
        elif check.name in ("scaffold-runtime", "git", "direnv"):
            section = "Runtime"
        else:
            section = {"mac": "macOS", "android": "Android", "shell": "Shell", "signing": "Signing"}.get(
                check.scopes[0], check.scopes[0])
        sections.setdefault(section, []).append(check)
    for section, items in sections.items():
        lines += ["", section]
        for check in items:
            note = "" if check.required else " (affects %s)" % ", ".join(check.affects or check.scopes)
            lines.append("%s  %s%s: %s" % (MARKERS[check.status], display_label(check.name), note, check.summary))
        if section == "Signing":
            lines.append("Signing configuration only; signing availability was not tested.")
    if missing:
        lines += ["", "Checkout", "❔  Checkout checks need a selected checkout. " + selection.summary]
    blocked = sum(check.required and check.status in (BLOCKER, UNSUPPORTED) for check in checks)
    incomplete = sum(check.required and check.status == NOT_CHECKED for check in checks)
    warnings = sum(check.status == WARNING for check in checks)
    lines.append("")
    if blocked:
        lines.append("Readiness blocked: %d blocker(s), %d required check(s) not checked, %d warning(s)." % (
            blocked, incomplete, warnings))
    elif incomplete:
        lines.append("Readiness incomplete: %s %d warning(s)." % (
            "Select a checkout to finish the checks." if missing else
            "%d required check(s) not checked." % incomplete, warnings))
    else:
        lines.append("Required checks passed; %d warning(s). Checks marked not checked remain unverified." % warnings)
    if hidden:
        lines.append("Checkout totals include the shared checks above.")
    repairs = []
    for check in checks:
        if check.name not in hidden and check.status != PASS:
            for step in check.repairs:
                if step not in repairs:
                    repairs.append(step)
    for step in repairs:
        command = shlex.join(step["argv"])
        if step.get("cwd"):
            command = "cd %s && %s" % (shlex.quote(step["cwd"]), command)
        note = " - " + step["note"] if step.get("note") else ""
        approval = " (requires you to act)" if step.get("requires_user_action") else ""
        lines.append("Next: %s%s%s" % (command, approval, note))
    lines += ["", MARKER_LEGEND]
    return "\n".join(lines)


def run_doctor(ctx):
    scopes_map = SCOPES
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
    if (any(group not in MACHINE_GROUPS for group in groups)
            and not ctx.parsed.get("checkout") and ctx.config.checkouts
            and ctx.identity(required=False, validate=False) is None):
        return all_checkout_reports(ctx, scopes, groups)
    return evaluate_checks(ctx, scopes, groups)


def all_checkout_reports(ctx, scopes, groups):
    """Inspect registered checkouts independently; shared caller-environment checks run once."""
    shared = {group: values for group, values in groups.items() if group in MACHINE_GROUPS}
    dependent = {group: values for group, values in groups.items() if group not in MACHINE_GROUPS}
    result = evaluate_checks(ctx, scopes, shared)
    reports = []
    shared_checks = [CheckResult(**check) for check in result.checks]
    for record in ctx.config.checkouts:
        label = record.alias or record.core
        try:
            identity = identity_module.build_identity(record.core_real, ctx.config, "configured", alias=record.alias)
            report = evaluate_checks(replace(ctx, selected=identity), scopes, dependent)
            report.context = identity.to_context()
        except ScaffoldError as error:
            check = _check("checkout-inspection", BLOCKER, error.message, scopes[0],
                           repairs=error.repairs, code=error.code, **error.details)
            report = error_result("doctor", readiness_error([check]))
            report.checks = [check.to_dict()]
            report.text = render_text(scopes, [check])
        reports.append({"alias": record.alias, "core": str(record.core_real),
                        "status": report.status, "exit_code": report.exit_code,
                        "checks": report.checks, "error": report.error, "warnings": report.warnings})
        for check in report.checks:
            tagged = dict(check, name="%s/%s" % (label, check["name"]))
            tagged["evidence"] = dict(check["evidence"], checkout=str(record.core_real), alias=record.alias)
            result.checks.append(tagged)
        result.warnings.extend(dict(warning, message="%s: %s" % (label, warning["message"]))
                               for warning in report.warnings)
    combined = [CheckResult(**check) for check in result.checks]
    error = readiness_error(combined)
    result.data["checkouts"] = reports
    if error:
        result.status, result.exit_code = "error", error.exit_code
        result.error = {"code": error.code, "message": error.message,
                        "details": error.details, "repairs": error.repairs}
    common, hidden = shared_checkout_checks(reports)
    texts = [render_text(scopes, [*shared_checks, *common],
                         heading="🩺 Brave setup doctor — shared checks").removesuffix("\n\n" + MARKER_LEGEND)]
    for report, names in zip(reports, hidden):
        checks = [CheckResult(**check) for check in report["checks"]]
        texts.append(render_text(scopes, checks, hidden=names, show_scopes=False,
                                 heading="Checkout: %s — %s" % (report["alias"] or report["core"], report["core"]))
                     .removesuffix("\n\n" + MARKER_LEGEND))
    texts.append("Overall: %s." % ("readiness blocked" if error and error.code == "READINESS_BLOCKED" else
                                     "readiness incomplete" if error else "required checks passed"))
    result.text = "\n\n".join([*texts, MARKER_LEGEND])
    return result



def shared_checkout_checks(reports):
    """Consolidate presentation only; every checkout retains its full readiness evidence."""
    names = {"host-macos-arm64", "xcode-developer-directory", "macos-sdk", "metal-toolchain",
             "adb", "git-lfs", "rbe-reachability"}
    common, hidden = [], [set() for _ in reports]
    if not reports:
        return common, hidden
    for check in reports[0]["checks"]:
        if check["name"] not in names:
            continue
        # Scope membership does not change the tool or environment being checked.
        comparable = {key: value for key, value in check.items() if key != "scopes"}
        if all(any({key: value for key, value in candidate.items() if key != "scopes"} == comparable
                   for candidate in report["checks"]) for report in reports):
            common.append(CheckResult(**check))
            for item in hidden:
                item.add(check["name"])
    disks = {}
    for index, report in enumerate(reports):
        for check in report["checks"]:
            device = check["evidence"].get("filesystem")
            if check["name"] != "disk-space" or device is None:
                continue
            hidden[index].add(check["name"])
            disks.setdefault(device, []).append((report, check))
    for entries in disks.values():
        # Use the lowest observed free space if the filesystem changed between probes.
        check = min((check for _, check in entries), key=lambda check: check["evidence"]["free_bytes"])
        labels = ", ".join(report["alias"] or report["core"] for report, _ in entries)
        common.append(replace(CheckResult(**check), summary=check["summary"] + " Checkouts: " + labels + "."))
    return common, hidden

def evaluate_checks(ctx, scopes, groups):
    checks = []
    state = checkout_state(ctx) if any(group not in MACHINE_GROUPS for group in groups) else CheckoutState()
    for group, group_scopes in groups.items():
        for check in group_checks(ctx, group, group_scopes[0], state):
            check.scopes = tuple(group_scopes)
            checks.append(check)
    if state.selection_error is not None and not any(check.name == "checkout-selection" for check in checks):
        error = state.selection_error
        checks.append(_check("checkout-selection", NOT_CHECKED, error.message, scopes[0], repairs=error.repairs))
    checks = merge_checks(checks)
    result = Result(command="doctor", data={"scopes": scopes})
    result.checks = [check.to_dict() for check in checks]
    for check in checks:
        if check.status != PASS and not (check.required and check.status != WARNING):
            result.add_warning("CHECK_" + check.status.upper(), "%s: %s" % (check.name, check.summary))
    result.text = render_text(scopes, checks)
    error = readiness_error(checks)
    if error is not None:
        failed = error_result("doctor", error, ctx.selected.to_context() if ctx.selected else None)
        failed.checks, failed.data, failed.warnings, failed.text = result.checks, result.data, result.warnings, result.text
        return failed
    return result
