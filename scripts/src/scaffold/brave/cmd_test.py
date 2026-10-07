# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""`test`: one named suite, the tests a branch or working tree changes, or the tests in named files."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from ..common.cli import Parsed
from ..common.results import Result, ScaffoldError
from . import android, android_tests, branch_tests, cmd_build, execution as execution_module


# Outcomes of running tests. Any other error is a setup problem that every later phase would hit too.
TEST_OUTCOME_CODES = ("CHILD_FAILED", "TEST_FAILED", "NO_TESTS_RAN", "ARTIFACT_MISSING")


def is_test_outcome(error):
    """A failed `apply_patches` shares CHILD_FAILED with a failing suite but leaves the tree for every later phase."""
    return error.code in TEST_OUTCOME_CODES and error.details.get("phase") != "patches"


def phase_context(ctx, phase, device):
    values = {**ctx.parsed.values, "suite": phase.suite, "target": phase.target, "filter": phase.filter,
              "base": None, "file": None, "device": device if phase.suite == "brave_java_unit_tests" else None}
    values["all_devices"] = False
    values["device_group"] = device if isinstance(device, android.DeviceGroup) and phase.suite == "brave_java_unit_tests" else None
    if isinstance(device, android.DeviceGroup):
        values["device"] = None
    return dataclasses.replace(ctx, parsed=Parsed(values=values, forwarded=list(ctx.parsed.forwarded),
                                                   delimiter=ctx.parsed.delimiter))


def failure_record(phase, error):
    return {"target": phase.target, "suite": phase.suite, "filter": phase.filter, "status": "failed",
            "error": {"code": error.code, "message": error.message}, "child_exit_code": error.child_exit_code,
            "operation_id": error.operation_id, "results": error.details.get("results"),
            "devices": error.details.get("devices")}


SCOPE_WORDS = {"both": "committed and working-tree changes", "committed": "committed changes",
               "worktree": "working-tree changes"}


def render_not_run(discovery):
    lines = []
    reasons = {}
    for path, reason in [*discovery.unmapped, *discovery.deselected]:
        reasons.setdefault(reason, []).append(path)
    for reason, paths in reasons.items():
        lines += ["", "Not run (%s):" % reason] + ["  " + path for path in paths]
    return lines


def render_summary(discovery, phase_results):
    lines = ["", "--------------------------------", "Test summary:"]
    width = max(len(p.suite) for p in discovery.phases)
    for phase, outcome in zip(discovery.phases, phase_results):
        counts = outcome.get("results")
        passed = outcome["status"] == "passed"
        marker = "✅" if passed else "❌"
        detail = "%s; test counts unavailable" % ("command succeeded" if passed else "failed")
        if counts:
            detail = "%d run, %d passed, %d failed, %d skipped" % (
                counts["ran"], counts["passed"], counts["failed"], counts["skipped"])
            if not passed:
                detail += "; phase failed"
        lines.append("  %s %-7s %-*s  %s" % (marker, phase.target, width, phase.suite, detail))
        lines.append("     --filter=%s" % phase.filter)
    for phase in discovery.phases[len(phase_results):]:
        lines.append("  ❌ %-7s %-*s  not run (stopped by an error)" % (phase.target, width, phase.suite))
    if len(phase_results) == len(discovery.phases) and all(
            p["status"] == "passed" and p.get("results") and p["results"]["ran"] > 0
            and p["results"]["failed"] == 0 for p in phase_results):
        lines.append("✅ All run tests passed.")
    elif len(phase_results) < len(discovery.phases):
        lines.append("❌ Test run incomplete: an error stopped the remaining suites.")
    elif any(p["status"] != "passed" for p in phase_results):
        failed = sum(p["status"] != "passed" for p in phase_results)
        lines.append("❌ %d of %d suite commands failed." % (failed, len(discovery.phases)))
    else:
        lines.append("✅ All suite commands succeeded; test counts are not fully verified.")
    lines += render_not_run(discovery)
    return "\n".join(lines)


def render_discovery(discovery):
    """What was found and what will run, as one aligned block (also saved to the log)."""
    if discovery.mode == "files":
        lines = ["Selected %d test file(s) from %d named file(s)" % (len(discovery.test_files), discovery.considered)]
    else:
        lines = ["Found %d modified test file(s) among %d changed files, against %s (%s)" % (
            len(discovery.test_files), discovery.considered, discovery.base, SCOPE_WORDS[discovery.scope])]
    if discovery.phases:
        width = max(len(p.suite) for p in discovery.phases)
        lines += ["", "Will run:"] + ["  %d. %-7s %-*s  --filter=%s" % (n, p.target, width, p.suite, p.filter)
                                      for n, p in enumerate(discovery.phases, 1)]
    else:
        lines += ["", "No tests were selected."]
    lines += render_not_run(discovery)
    return "\n".join(lines) + "\n"


def checkout_files(ctx, identity, tokens):
    """Checkout-relative paths for caller-supplied files; relative tokens resolve from the caller's directory."""
    core = Path(os.path.realpath(identity.core))
    found = []
    for token in tokens:
        resolved = Path(os.path.realpath(os.path.join(ctx.cwd, os.path.expanduser(token))))
        if core != resolved and core not in resolved.parents:
            raise ScaffoldError("INVALID_INPUT", "%s is outside the selected checkout (%s)." % (token, core),
                                details={"file": token, "checkout": str(core)})
        if not resolved.is_file():
            raise ScaffoldError("INVALID_INPUT", "%s is not a file in the selected checkout." % token,
                                details={"file": token})
        relative = resolved.relative_to(core).as_posix()
        if relative not in found:
            found.append(relative)
    return found


def cmd_test(ctx):
    """A named suite keeps the single-suite path; otherwise tests are discovered."""
    if ctx.parsed.get("suite"):
        return cmd_build.cmd_test(ctx)
    return cmd_test_discovered(ctx)


def cmd_test_discovered(ctx):
    parsed = ctx.parsed
    if parsed.get("all_devices") and parsed.get("device"):
        raise ScaffoldError("SELECTOR_CONFLICT", "Use either --device or --all-devices, not both.")
    identity, effective = cmd_build.select_build(ctx, parsed.get("target"), parsed.forwarded, tests=True)
    selected_target = bool(parsed.get("target")) or effective.sources["target"] == "forwarded"
    if parsed.get("file"):
        discovery = branch_tests.discover_files(identity.core, checkout_files(ctx, identity, [parsed.get("file")]), ctx.log)
        scope_target = effective.target if selected_target else None
    else:
        discovery = branch_tests.discover(identity.core, parsed.get("base") or branch_tests.DEFAULT_BASE, "both", ctx.log)
        scope_target = effective.target
    if scope_target:
        discovery.deselected = [(f, "%s tests are outside the requested %s run" % (p.target, scope_target))
                                for p in discovery.phases if p.target != scope_target for f in p.files]
        discovery.phases = [p for p in discovery.phases if p.target == scope_target]
    ctx.log.phase(render_discovery(discovery))
    result = Result(command="test")
    result.data = {"discovery": discovery.to_dict(), "phases": []}
    if not discovery.phases:
        result.text = "No tests were selected; nothing was run."
        return result
    if parsed.get("plan"):
        result.text = "Plan only: nothing was run."
        return result
    device = None
    if any(p.target == "android" for p in discovery.phases):
        android_tests.require_support_branch(identity, ctx.log)
    if any(p.suite == "brave_java_unit_tests" for p in discovery.phases):
        execution = execution_module.load(ctx, identity)
        choice = android.preflight_deployment(execution.context(ctx), effective.arch)
        device = choice if isinstance(choice, android.DeviceGroup) else choice[1]["id"]
    phase_results, failures = [], []
    for number, phase in enumerate(discovery.phases, 1):
        ctx.log.phase("Phase %d/%d: %s %s" % (number, len(discovery.phases), phase.target, phase.suite))
        try:
            done = cmd_build.cmd_test(phase_context(ctx, phase, device))
            if done.error:
                error = ScaffoldError(done.error["code"], done.error["message"],
                                      details={**done.error.get("details", {}), "results": (done.data or {}).get("results")},
                                      child_exit_code=done.child_exit_code)
                error.operation_id = done.operation_id
                raise error
        except ScaffoldError as error:
            if not is_test_outcome(error):
                error.details.setdefault("test_phases", phase_results)
                error.details.setdefault("not_run", [p.suite for p in discovery.phases[number - 1:]])
                error.message += "\n\n" + render_summary(discovery, phase_results)
                raise
            failures.append(failure_record(phase, error))
            phase_results.append(failures[-1])
            ctx.log.phase("Phase failed: %s: %s" % (error.code, error.message))
            continue
        phase_results.append({"target": phase.target, "suite": phase.suite, "filter": phase.filter, "status": "passed",
                              "operation_id": done.operation_id, "results": (done.data or {}).get("results"),
                              "devices": (done.data or {}).get("devices")})
        result.warnings += done.warnings
    result.data["phases"] = phase_results
    if failures:
        raise ScaffoldError(
            "CHILD_FAILED", "%d of %d test phase(s) failed:\n%s" % (
                len(failures), len(phase_results), "\n".join(
                    "  %s %s: %s" % (f["target"], f["suite"], f["error"]["message"]) for f in failures))
            + "\n\n" + render_summary(discovery, phase_results),
            details={"phases": phase_results, "discovery": discovery.to_dict()},
            child_exit_code=next((f["child_exit_code"] for f in failures if f["child_exit_code"]), None))
    result.child_exit_code = 0
    result.text = render_summary(discovery, phase_results)
    ctx.log.save(result.text + "\n")
    return result
