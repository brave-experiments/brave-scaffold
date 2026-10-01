# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""`test-local`: run the tests a branch or working tree modifies, one suite after another."""

from __future__ import annotations

import dataclasses

from ..common.cli import Parsed
from ..common.results import Result, ScaffoldError
from . import android, android_tests, branch_tests, cmd_build, execution as execution_module


def phase_context(ctx, phase, device):
    values = {**ctx.parsed.values, "suite": phase.suite, "target": phase.target, "filter": phase.filter,
              "base": None, "scope": None, "device": device if phase.suite == "brave_java_unit_tests" else None}
    return dataclasses.replace(ctx, parsed=Parsed(values=values, delimiter=ctx.parsed.delimiter))


def failure_record(phase, error):
    return {"target": phase.target, "suite": phase.suite, "filter": phase.filter, "status": "failed",
            "error": {"code": error.code, "message": error.message}, "child_exit_code": error.child_exit_code,
            "operation_id": error.operation_id}


def cmd_test_local(ctx):
    parsed = ctx.parsed
    identity = ctx.identity()
    discovery = branch_tests.discover(identity.core, parsed.get("base") or branch_tests.DEFAULT_BASE,
                                      parsed.get("scope") or "both", ctx.log)
    ctx.log.phase("Modified tests against %s (%s): %d of %d changed files" % (
        discovery.base, discovery.scope, len(discovery.test_files), discovery.considered))
    for phase in discovery.phases:
        ctx.log.phase("  %s %s --filter=%s" % (phase.target, phase.suite, phase.filter))
    for path, reason in discovery.unmapped:
        ctx.log.phase("  not run: %s (%s)" % (path, reason))
    result = Result(command="test-local")
    result.data = {"discovery": discovery.to_dict(), "phases": []}
    for path, reason in discovery.unmapped:
        result.add_warning("TEST_UNMAPPED", "%s: %s" % (path, reason))
    if not discovery.phases:
        result.text = "No modified tests could be mapped to a suite; nothing was run."
        return result
    if parsed.get("plan"):
        result.text = "Would run (nothing was run):\n" + "\n".join(
            "  bdev test %s %s --filter=%s" % (p.target, p.suite, p.filter) for p in discovery.phases)
        return result
    device = None
    if any(p.target == "android" for p in discovery.phases):
        android_tests.require_support_branch(identity, ctx.log)
    if any(p.suite == "brave_java_unit_tests" for p in discovery.phases):
        execution = execution_module.load(ctx, identity)
        device = android.preflight_device(execution.context(ctx))[1]["id"]
    phase_results, failures = [], []
    for number, phase in enumerate(discovery.phases, 1):
        ctx.log.phase("Test phase %d/%d: %s %s --filter=%s" % (number, len(discovery.phases), phase.target, phase.suite,
                                                                phase.filter))
        try:
            done = cmd_build.cmd_test(phase_context(ctx, phase, device))
        except ScaffoldError as error:
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
            "CHILD_FAILED", "%d of %d test phase(s) failed: %s." % (
                len(failures), len(phase_results), ", ".join("%s %s" % (f["target"], f["suite"]) for f in failures)),
            details={"phases": phase_results, "discovery": discovery.to_dict()},
            child_exit_code=next((f["child_exit_code"] for f in failures if f["child_exit_code"]), None))
    result.child_exit_code = 0
    result.text = "All %d test phase(s) passed: %s." % (
        len(phase_results), ", ".join("%s %s" % (p["target"], p["suite"]) for p in phase_results))
    return result
