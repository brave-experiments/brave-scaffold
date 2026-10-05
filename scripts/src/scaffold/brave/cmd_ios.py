# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Build and run for the iOS Simulator.

Core builds iOS through Xcode, not through its package build command: the Debug scheme's pre-action runs
Core's own build for BraveCore. The scaffold therefore runs `xcodebuild` and forwards unknown arguments to it.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..common import tools as tools_module
from ..common.results import Cancelled, Result, ScaffoldError, repair
from . import cmd_build, execution as execution_module, freshness, ios, packages, patches, steps as step_module
from . import sync as sync_module
from .records import OutputState, output_states, track


def choose_simulator(environ, log, identity, requested):
    _, devices = ios.list_simulators(environ, log)
    minimum = ios.deployment_target(ios.project_path(identity) / "project.pbxproj")
    return ios.select_simulator(devices, requested, minimum)


def simulator_data(simulator):
    return {key: simulator[key] for key in ("udid", "name", "version", "state")}


# --- build ----------------------------------------------------------------------------


def do_build(ctx, command, sync_first=False, run_after=False):
    parsed = ctx.parsed
    identity = ctx.identity()
    build = ios.resolve_build(parsed, identity, run_after)
    if parsed.get("plan"):
        return plan_build(ctx, command, identity, build, sync_first, run_after)
    execution = execution_module.load(ctx, identity)
    execution = cmd_build.prepare(ctx, identity, "ios", "sync" if sync_first else "build", False, execution)

    def pick(execution):
        if build.destination_forwarded:
            return None
        return choose_simulator(execution.environ, ctx.log, identity, build.requested_device)

    simulator = None if sync_first else pick(execution)
    with track(ctx, command, identity, build.describe(), validated=True) as op:
        sync_result = None
        if sync_first:
            sync_result = sync_module.do_sync_phase(ctx, execution, op, "ios", [])
            execution = cmd_build.prepare_for_build(execution, ctx, "ios", False)
            simulator = pick(execution)
        outcome = perform_build(ctx, execution, build, op, simulator)
        if run_after and outcome["artifact"] is None:
            raise unresolved_error(outcome, identity)
        result = finish_build_result(command, outcome, execution, op)
        if sync_result:
            result.data["sync"] = sync_result
        if run_after:
            result = run_phase(ctx, execution, outcome["artifact"], simulator, result, op)
        return op.complete(result)


def perform_build(ctx, execution, build, op, simulator):
    identity = execution.identity
    ctx.log.phase("Building Brave for the iOS Simulator (%s, %s)" % (ios.CONFIGURATION, ios.ARCH))
    ctx.log.phase("GN output directory (built by Core's scheme pre-action): %s" % build.gn_output)
    ctx.log.phase("Xcode products directory: %s" % build.derived_data)
    with ctx.log.measure("Source preparation"):
        patches_applied, patch_plan = cmd_build.prepare_patches(ctx, execution, op)
    argv = ios.xcodebuild_argv(build, simulator)
    states = [OutputState(identity, path, ctx.state_root) for path in build.outputs]
    for state in states:
        state.begin_attempt(op.id, build.changes_output)
    step = step_module.xcodebuild_step(identity, build, argv, ["patch-preparation"])
    op.start("xcodebuild", **step.record())
    op.detail(effective=build.describe(), xcodebuild_arguments=argv[1:])
    try:
        with ctx.log.measure("xcodebuild"):
            code = packages.run_argv(ctx, execution, argv, identity.core)
    except Cancelled:
        for state in states:
            state.end_attempt(op.id, "cancelled")
        raise
    if code != 0:
        for state in states:
            state.end_attempt(op.id, "failed")
        op.fail("xcodebuild", exit=code)
        raise ScaffoldError("CHILD_FAILED", "xcodebuild exited with status %d." % code,
                            details={"argv": argv, "cwd": str(identity.core), "output_dir": str(build.derived_data)},
                            child_exit_code=code)
    op.succeed("xcodebuild", exit=0)
    op.start("verify-output", output_dir=str(build.derived_data))
    try:
        with ctx.log.measure("Output verification"):
            artifact, reason = ios.verify_artifact(identity, build)
    except ScaffoldError as error:
        op.fail("verify-output", code=error.code)
        for state in states:
            state.end_attempt(op.id, "output-invalid")
        error.child_exit_code = 0
        error.details.setdefault("argv", argv)
        raise
    op.succeed("verify-output", artifact_status="verified" if artifact else "unresolved",
               verified_output=artifact["output_dir"] if artifact else None, explanation=reason)
    if artifact:
        op.attach_artifacts([artifact])
    with ctx.log.measure("Source state"):
        inputs = freshness.compute(identity, patch_plan.report.patched_paths, argv, ctx.log)
    for state in states:
        if artifact is not None:
            state.record_success(op.id, artifact, inputs)
        else:
            state.end_attempt(op.id, "unverified")
    return {"argv": argv, "artifact": artifact, "reason": reason, "build": build, "patches_applied": patches_applied}


def build_data(outcome, identity):
    artifact = outcome["artifact"]
    return {"child_succeeded": True, "artifact_status": "verified" if artifact else "unresolved",
            "argv": outcome["argv"], "cwd": str(identity.core), "effective": outcome["build"].describe(),
            "patches_applied": outcome["patches_applied"],
            "verified_output": artifact["output_dir"] if artifact else None,
            "explanation": None if artifact else
            "xcodebuild completed but no artifact was verified: %s." % outcome["reason"]}


def unresolved_error(outcome, identity):
    return ScaffoldError(
        "ARTIFACT_UNRESOLVED", "xcodebuild completed but no artifact was verified (%s). Nothing was installed or "
        "launched." % outcome["reason"], details={"build": build_data(outcome, identity), "phase": "build"},
        child_exit_code=0)


def finish_build_result(command, outcome, execution, op):
    identity = execution.identity
    result = Result(command=command, operation_id=op.id, child_exit_code=0,
                    checks=[check.to_dict() for check in execution.checks])
    result.data = {"build": build_data(outcome, identity)}
    if outcome["artifact"]:
        result.artifacts = [outcome["artifact"]]
        result.text = "✅ Build completed.\nVerified output: %s" % outcome["artifact"]["path"]
    else:
        result.add_warning("ARTIFACT_UNRESOLVED", result.data["build"]["explanation"])
        result.text = result.data["build"]["explanation"]
    return result


def plan_build(ctx, command, identity, build, sync_first, run_after):
    sync_arguments = sync_module.sync_arguments(ctx, "ios", [], identity) if sync_first else None
    toolchain, steps = cmd_build.common_plan_steps(ctx, identity, "ios", "sync" if sync_first else "build", False)
    last = "readiness"
    if sync_arguments is not None:
        steps.append(sync_module.plan_step(identity, sync_arguments, toolchain, ["readiness"]))
        steps.append(step_module.Step(
            "readiness-after-sync", "Check tools and build readiness again once the sync has changed the checkout.",
            "unresolved", needs=["sync"], on_failure="The build does not start.",
            detail="Judged after the sync ran; it cannot be known before."))
        last = "readiness-after-sync"
    try:
        patch_plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root)
    except ScaffoldError as error:
        patch_plan = error
    apply_argv = tools_module.package_argv(toolchain, ["run", "apply_patches"]) if toolchain else None
    patch_step = step_module.patches_step(identity, patch_plan, apply_argv, after_sync=sync_arguments is not None)
    patch_step.needs = [last]
    steps.append(patch_step)
    simulator, error = None, None
    if not build.destination_forwarded:
        if sync_first:
            error = ScaffoldError("DEVICE_UNAVAILABLE", "Chosen after the sync ran; it cannot be known before.")
        else:
            try:
                simulator = choose_simulator(ctx.environ, ctx.log, identity, build.requested_device)
            except ScaffoldError as caught:
                error = caught
        steps.append(step_module.select_simulator_step(simulator, error, ["patch-preparation"]))
    steps.append(step_module.xcodebuild_step(identity, build, ios.xcodebuild_argv(build, simulator),
                                             ["select-simulator" if not build.destination_forwarded
                                              else "patch-preparation"]))
    steps.append(step_module.verify_ios_step(build, ["xcodebuild"]))
    if run_after:
        steps += run_steps(build.app_path, simulator, ["verify-output"], None)
    return plan_result(command, build, steps)


def plan_result(command, build, steps):
    result = Result(command=command)
    result.data = {"plan": {"steps": [step.to_dict() for step in steps], "effective": build.describe()}}
    result.text = step_module.render_plan(command, steps)
    return result


def run_steps(app, simulator, needs, bundle):
    udid = simulator["udid"] if simulator else "<simulator>"
    bundle_id = bundle["bundle_identifier"] if bundle else "<bundle identifier from the built app>"
    return [step_module.boot_simulator_step(udid, needs),
            step_module.install_app_step(udid, str(app), ["boot-simulator"]),
            step_module.launch_app_step(udid, bundle_id, ["install-app"])]


# --- run ------------------------------------------------------------------------------


def run_phase(ctx, execution, bundle, simulator, result, op=None):
    """Install the verified app on the chosen simulator and launch it."""
    descriptions = {step.name: step for step in run_steps(bundle["path"], simulator, [], bundle)}
    if op is not None:
        op.start("run", artifact=bundle["path"])

    def start_phase(name):
        if op is not None:
            op.start(name, **descriptions[name].record())

    outcome = ios.restart_app(simulator["udid"], bundle, execution.environ, ctx.log,
                              progress=op.succeed if op is not None else None, started=start_phase,
                              failed=op.fail if op is not None else None)
    if op is not None:
        op.succeed("run", artifact=bundle["path"])
    result.data = {**(result.data or {}), "run": {"artifact": bundle, "simulator": simulator_data(simulator),
                                                  "launched_pid": outcome["launched_pid"],
                                                  "bundle_identifier": outcome["bundle_identifier"]}}
    if not result.artifacts:
        result.artifacts = [{**bundle, "verified": False}]
    result.text = ((result.text + "\n") if result.text else "") + "Launched %s on %s (%s)." % (
        bundle["name"], simulator["name"], simulator["udid"])
    return result


def run_candidates(ctx, identity):
    found = {}
    paths = [ios.app_in(ios.default_derived_data(identity))]
    for state in output_states(identity, ctx.state_root):
        artifact = (state.get("success") or {}).get("artifact") or {}
        if artifact.get("target") == "ios" and artifact.get("configuration") == ios.CONFIGURATION:
            paths.append(Path(artifact["path"]))
    for path in paths:
        try:
            found[os.path.realpath(path)] = ios.read_bundle(path)
        except ScaffoldError:
            continue
    return list(found.values())


def select_artifact(ctx, identity):
    explicit = ctx.parsed.get("artifact")
    if explicit:
        path = Path(os.path.expanduser(explicit))
        path = path if path.is_absolute() else Path(ctx.cwd) / path
        return ios.read_bundle(Path(os.path.normpath(path)))
    candidates = run_candidates(ctx, identity)
    if not candidates:
        raise ScaffoldError(
            "ARTIFACT_MISSING", "No iOS Simulator application was found for this checkout.",
            details={"searched": str(ios.app_in(ios.default_derived_data(identity)))},
            repairs=[repair(["bdev", "build", "ios", "--checkout", str(identity.core)]),
                     repair(["bdev", "build-run", "ios", "--checkout", str(identity.core)])])
    if len(candidates) > 1:
        raise ScaffoldError(
            "ARTIFACT_AMBIGUOUS", "More than one application matches; choose one with --artifact.",
            details={"candidates": [item["path"] for item in candidates]},
            repairs=[repair(["bdev", "run", "ios", "--artifact", candidates[0]["path"]],
                            note="Example only; choose the application you intend to run.")])
    return candidates[0]


def cmd_run(ctx, identity):
    parsed = ctx.parsed
    if parsed.get("configuration") not in (None, "debug"):
        raise ScaffoldError("UNSUPPORTED_CAPABILITY", "iOS supports Debug simulator builds only.")
    bundle = select_artifact(ctx, identity)
    if parsed.get("plan"):
        try:
            simulator = choose_simulator(ctx.environ, ctx.log, identity, parsed.get("device"))
            chosen = step_module.select_simulator_step(simulator, needs=["environment"])
        except ScaffoldError as error:
            simulator, chosen = None, step_module.select_simulator_step(error=error, needs=["environment"])
        steps = [step_module.plan_environment_step(ctx, identity),
                 step_module.Step("select-artifact", "Use the selected application; run never builds.", "resolved",
                                  reads=[bundle["path"]], detail=bundle["path"], needs=["environment"]),
                 chosen, *run_steps(bundle["path"], simulator, ["select-simulator"], bundle)]
        result = Result(command="run", data={"plan": {"artifact": bundle, "steps": [s.to_dict() for s in steps]}})
        result.text = step_module.render_plan("run", steps)
        return result
    execution = execution_module.load(ctx, identity)
    simulator = choose_simulator(execution.environ, ctx.log, identity, parsed.get("device"))
    with track(ctx, ctx.command, identity, {"target": "ios", "artifact": bundle["path"]}, validated=True) as op:
        op.start("select-simulator", **step_module.select_simulator_step(simulator).record())
        op.succeed("select-simulator", simulator=simulator["udid"])
        return op.complete(run_phase(ctx, execution, bundle, simulator, Result(command=ctx.command), op))
