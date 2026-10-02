# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""`test-local`: run the tests a branch or working tree modifies, one suite after another."""

from __future__ import annotations

import dataclasses

from ..common.cli import Parsed
from ..common.platforms import normalize_target
from ..common.results import Result, ScaffoldError
from . import android, android_tests, branch_tests, cmd_build, execution as execution_module


# Outcomes of running tests. Any other error is a setup problem that every later phase would hit too.
TEST_OUTCOME_CODES = ("CHILD_FAILED", "TEST_FAILED", "NO_TESTS_RAN", "ARTIFACT_MISSING")


def phase_context(ctx, phase, device):
    values = {**ctx.parsed.values, "suite": phase.suite, "target": phase.target, "filter": phase.filter,
              "base": None, "scope": None, "device": device if phase.suite == "brave_java_unit_tests" else None}
    return dataclasses.replace(ctx, parsed=Parsed(values=values, delimiter=ctx.parsed.delimiter))


def failure_record(phase, error):
    return {"target": phase.target, "suite": phase.suite, "filter": phase.filter, "status": "failed",
            "error": {"code": error.code, "message": error.message}, "child_exit_code": error.child_exit_code,
            "operation_id": error.operation_id, "results": error.details.get("results")}


SCOPE_WORDS = {"both": "committed and working-tree changes", "committed": "committed changes",
               "worktree": "working-tree changes"}


def render_not_run(discovery):
    lines = []
    reasons = {}
    for path, reason in discovery.unmapped:
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
        marker = "✅" if passed and counts and counts["ran"] > 0 and counts["failed"] == 0 else "  "
        detail = "%s; counts unavailable" % outcome["status"]
        if counts:
            detail = "%d run, %d passed, %d failed, %d skipped" % (
                counts["ran"], counts["passed"], counts["failed"], counts["skipped"])
            if not passed:
                detail += "; phase failed"
        lines.append("  %s %-7s %-*s  %s" % (marker, phase.target, width, phase.suite, detail))
        lines.append("     --filter=%s" % phase.filter)
    for phase in discovery.phases[len(phase_results):]:
        lines.append("     %-7s %-*s  not run" % (phase.target, width, phase.suite))
    if len(phase_results) == len(discovery.phases) and all(
            p["status"] == "passed" and p.get("results") and p["results"]["ran"] > 0
            and p["results"]["failed"] == 0 for p in phase_results):
        lines.append("✅ All run tests passed.")
    lines += render_not_run(discovery)
    return "\n".join(lines)


def render_discovery(discovery):
    """What was found and what will run, as one aligned block (also saved to the log)."""
    lines = ["Found %d modified test file(s) among %d changed files, against %s (%s)" % (
        len(discovery.test_files), discovery.considered, discovery.base, SCOPE_WORDS[discovery.scope])]
    if discovery.phases:
        width = max(len(p.suite) for p in discovery.phases)
        lines += ["", "Will run:"] + ["  %d. %-7s %-*s  --filter=%s" % (n, p.target, width, p.suite, p.filter)
                                      for n, p in enumerate(discovery.phases, 1)]
    lines += render_not_run(discovery)
    return "\n".join(lines) + "\n"


def cmd_test_local(ctx):
    parsed = ctx.parsed
    identity = ctx.identity()
    discovery = branch_tests.discover(identity.core, parsed.get("base") or branch_tests.DEFAULT_BASE,
                                      parsed.get("scope") or "both", ctx.log)
    if parsed.positionals:
        target = normalize_target(parsed.positionals[0])
        if target not in ("mac", "android"):
            raise ScaffoldError("INVALID_INPUT", "%r is not a test-local target; use mac or android." % parsed.positionals[0],
                                details={"example": "bdev test-local android"})
        discovery.phases = [phase for phase in discovery.phases if phase.target == target]
    ctx.log.phase(render_discovery(discovery))
    result = Result(command="test-local")
    result.data = {"discovery": discovery.to_dict(), "phases": []}
    if not discovery.phases:
        result.text = "No modified %stests could be mapped to a suite; nothing was run." % (
            parsed.positionals[0] + " " if parsed.positionals else "")
        return result
    if parsed.get("plan"):
        result.text = "Plan only: nothing was run."
        return result
    device = None
    if any(p.target == "android" for p in discovery.phases):
        android_tests.require_support_branch(identity, ctx.log)
    if any(p.suite == "brave_java_unit_tests" for p in discovery.phases):
        execution = execution_module.load(ctx, identity)
        device = android.preflight_device(execution.context(ctx))[1]["id"]
    phase_results, failures = [], []
    for number, phase in enumerate(discovery.phases, 1):
        ctx.log.phase("Phase %d/%d: %s %s" % (number, len(discovery.phases), phase.target, phase.suite))
        try:
            done = cmd_build.cmd_test(phase_context(ctx, phase, device))
        except ScaffoldError as error:
            if error.code not in TEST_OUTCOME_CODES:
                error.details.setdefault("test_phases", phase_results)
                error.details.setdefault("not_run", [p.suite for p in discovery.phases[number - 1:]])
                error.message += "\n\n" + render_summary(discovery, phase_results)
                raise
            failures.append(failure_record(phase, error))
            phase_results.append(failures[-1])
            ctx.log.phase("Phase failed: %s: %s" % (error.code, error.message))
            continue
        phase_results.append({"target": phase.target, "suite": phase.suite, "filter": phase.filter, "status": "passed",
                              "operation_id": done.operation_id, "results": (done.data or {}).get("results")})
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
