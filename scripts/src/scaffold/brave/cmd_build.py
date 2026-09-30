# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Sync, build, test, run, and their combinations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..common import env as env_module
from ..common import tools as tools_module
from ..common.checks import readiness_error
from ..common.platforms import RECOGNIZED_TARGETS, effective_target, normalize_target
from ..common.procs import run_capture
from ..common.redaction import redact_argv
from ..common.results import Cancelled, Result, ScaffoldError, repair
from . import android_deps, buildopts, execution as execution_module, freshness, macos, packages, patches, steps as step_module, sync_scope
from .records import OutputState, output_states, track

SUITE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*_tests?")
COMMON_SUITES = ("brave_browser_tests", "brave_unit_tests", "brave_all_unit_tests", "chromium_unit_tests",
                 "browser_tests")


# --- selection and gates -------------------------------------------------------------


def requested_configuration(ctx):
    value = ctx.parsed.get("configuration")
    return value.capitalize() if value else None


def select_build(ctx, target_token, forwarded):
    """Resolve identity and the effective build choices; conflicts fail before any effect."""
    identity = ctx.identity()
    target, _ = effective_target(target_token, ctx.config)
    explicit = normalize_target(target_token) if target_token else None
    configuration = requested_configuration(ctx)
    effective = buildopts.resolve_effective(
        identity.src, forwarded, target, configuration or "Debug", explicit, configuration,
        bool(ctx.parsed.get("offline")))
    require_available_target(effective.target)
    return identity, effective


def require_available_target(target):
    if target == "android":
        from . import android
        android.require_available()
    elif target != "mac":
        raise ScaffoldError("UNSUPPORTED_CAPABILITY", "The target %r is not available." % target)


def readiness_checks(ctx, target, phase="build", remote_required=False):
    """The readiness checks for one phase, without judging them.

    The sync phase needs the macOS host but not the Android target it is about to establish, and it does not
    demand the remote-build configuration that sync itself refreshes. The build phase needs the target and, when
    the effective compile mode is remote, the local remote-build configuration.
    """
    from . import doctor
    checks = []
    groups = ("host-mac", "mac-build") if target == "mac" or phase == "sync" else ("android-build",)
    for group in groups:
        function = doctor.GROUP_FUNCTIONS[group]
        scope = "mac" if group != "android-build" else "android"
        if group in ("mac-build", "android-build"):
            checks.extend(function(ctx, scope, remote_required=remote_required and phase == "build"))
        else:
            checks.extend(function(ctx, scope))
    return checks


def readiness_gate(ctx, target, phase="build", remote_required=False):
    """Required checks for one phase; blockers stop before that phase prepares anything."""
    checks = readiness_checks(ctx, target, phase, remote_required)
    error = readiness_error(checks)
    if error is not None:
        error.details["checks"] = [check.to_dict() for check in checks if check.status != "pass"]
        raise error
    return checks


def prepare(ctx, identity, target, phase="build", remote_required=False, execution=None):
    """Environment, checkout-local tools, and readiness for one phase, as one execution context."""
    execution = execution or execution_module.load(ctx, identity)
    execution = execution_module.resolve_tools(execution, ctx)
    return execution.with_checks(readiness_gate(execution.context(ctx), target, phase, remote_required))


def prepare_for_build(execution, ctx, target, remote_required):
    """After sync changed the checkout: resolve its tools and judge the build's readiness again."""
    execution = execution_module.resolve_tools(execution, ctx)
    return execution.with_checks(readiness_gate(execution.context(ctx), target, "build", remote_required))


def metal_environment(ctx, environ):
    """Point the build at an installed Metal toolchain when `xcrun metal` cannot find one."""
    probe = run_capture(["xcrun", "metal", "--version"], os.getcwd(), environ, ctx.log, timeout=60)
    if probe.returncode == 0:
        return {}
    for info in sorted(Path("/private/var/run/com.apple.security.cryptexd/mnt").glob(
            "com.apple.MobileAsset.MetalToolchain-*/Metal.xctoolchain/ToolchainInfo.plist")):
        result = run_capture(["/usr/libexec/PlistBuddy", "-c", "Print :Identifier", str(info)], os.getcwd(),
                             environ, ctx.log, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            existing = environ.get("TOOLCHAINS")
            identifier = result.stdout.strip()
            return {"TOOLCHAINS": "%s:%s" % (existing, identifier) if existing else identifier}
    return {}


# --- patch preparation -----------------------------------------------------------------


def prepare_patches(ctx, execution, op):
    """Apply Core patches only when needed and only when no local work is at risk."""
    identity = execution.identity
    plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root)
    apply_argv = tools_module.package_argv(execution.toolchain, ["run", "apply_patches"])
    op.step("patch-preparation-plan", action=plan.action, reason=plan.reason,
            **step_module.patches_step(identity, plan, apply_argv).record())
    if plan.action == "current":
        state = patches.snapshot_files(identity, plan.report)
        patches.write_receipt(identity, plan.trees, state, ctx.state_root)
        return False, plan
    if plan.action == "conflict":
        raise patches.conflict_error(plan, identity)
    op.step("apply-patches", **step_module.patches_step(identity, plan, apply_argv).record())
    argv, code = packages.run(ctx, execution, ["run", "apply_patches"])
    if code != 0:
        raise ScaffoldError("CHILD_FAILED", "Applying Core patches failed (exit %d)." % code,
                            details={"argv": argv, "phase": "patches"}, child_exit_code=code)
    after = patches.collect_drift(identity)
    if after.files:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Patches were applied but %d file(s) still differ from their metadata." % len(after.files),
                            details={"files": sorted(after.files)[:50]},
                            repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)])])
    patches.write_receipt(identity, plan.trees, patches.snapshot_files(identity, after), ctx.state_root,
                          patches.core_output(identity))
    return True, plan


# --- artifacts ---------------------------------------------------------------------


def verify_mac_artifact(effective):
    """Return (artifact_dict, None) or (None, reason) when the identity cannot be established."""
    if effective.unresolved:
        return None, "; ".join(effective.unresolved)
    path = macos.app_path(effective.output_dir, effective.configuration, effective.channel)
    bundle = macos.read_bundle(path)
    bundle.update(kind="app", target="mac", configuration=effective.configuration, arch=effective.arch,
                  output_dir=str(effective.output_dir), verified=True)
    return bundle, None


def artifact_for(effective, identity, environ, log=None):
    if effective.target == "mac":
        return verify_mac_artifact(effective)
    return _android().verify_artifact(effective, identity, environ, log)


def _android():
    from . import android
    return android


# --- build phase ---------------------------------------------------------------------


FORCE_GN_ARGUMENT = "--force_gn_gen"


def build_arguments(effective, subcommand, script_args, force_gn):
    arguments = ["run", subcommand, *script_args, *effective.generated]
    if force_gn:
        arguments.append(FORCE_GN_ARGUMENT)
    return [*arguments, *effective.forwarded]


@dataclass
class BuildOutcome:
    argv: list
    child_exit: int
    artifact: dict | None
    unresolved_reason: str | None
    effective: object
    prepared_patches: bool


def run_output_step(ctx, execution, effective, op, arguments, phase, extra_env=None, before_child=None):
    """Run the package command that writes the output directory.

    The attempt is recorded before the child starts, so an earlier success stops
    being proof of the output's contents even if this process dies mid-write.
    """
    identity = execution.identity
    state = OutputState(identity, effective.output_dir, ctx.state_root) if effective.output_dir else None
    if state is not None:
        state.begin_attempt(op.id, effective.changes_output)
    argv_plan = tools_module.package_argv(execution.toolchain, arguments)
    op.step(phase, package_arguments=arguments,
            **step_module.build_step(identity, effective, phase, arguments, argv_plan, ["patch-preparation"]).record())
    try:
        if before_child is not None:
            before_child()
        argv, code = packages.run(ctx, execution, arguments, extra_env)
    except Cancelled:
        if state is not None:
            state.end_attempt(op.id, "cancelled")
        raise
    if code != 0 and state is not None:
        state.end_attempt(op.id, "failed")
    if code != 0:
        raise ScaffoldError("CHILD_FAILED", "The package %s command exited with status %d." % (phase, code),
                            details={"argv": argv, "cwd": str(identity.core), "output_dir": str(effective.output_dir)},
                            child_exit_code=code)
    return argv, state


def perform_build(ctx, execution, effective, op, force_gn=False):
    """Prepare sources, run the package build, then verify the resulting output."""
    identity = execution.identity
    changed, plan = prepare_patches(ctx, execution, op)
    android = effective.target == "android"
    if android:
        refreshed = _android().prepare_support(ctx, execution, op, effective)
        if refreshed:
            patches.record_extra_expected(identity, ctx.state_root)
        changed = refreshed or changed
    arguments = build_arguments(effective, "build", (), force_gn or changed)
    if android:
        described = step_module.gn_step(effective, effective.output_dir / "args.gn", effective.chosen_gn_keys)
        op.step(described.name, **described.record())
    op.update(effective={"target": effective.target, "configuration": effective.configuration,
                         "arch": effective.arch, "output_dir": str(effective.output_dir),
                         "package_arguments": arguments})
    argv, state = run_output_step(
        ctx, execution, effective, op, arguments, "build",
        _android().build_environment(execution.context(ctx)) if android else metal_environment(ctx, execution.environ),
        (lambda: _android().write_gn_overrides(identity, effective)) if android else None)
    try:
        artifact, reason = artifact_for(effective, identity, execution.environ, ctx.log)
    except ScaffoldError as error:
        if state is not None:
            state.end_attempt(op.id, "output-invalid")
        error.child_exit_code = 0
        error.details.setdefault("argv", argv)
        raise
    inputs = freshness.compute(identity, plan.report.patched_paths, arguments, ctx.log,
                               android_deps.freshness_inputs(identity, ctx.log) if android else None)
    if artifact is not None and state is not None:
        state.record_success(op.id, artifact, inputs)
    elif state is not None:
        state.end_attempt(op.id, "unverified")
    return BuildOutcome(argv, 0, artifact, reason, effective, changed)


def build_data(outcome, identity):
    effective = outcome.effective
    return {"child_succeeded": True, "artifact_status": "verified" if outcome.artifact else "unresolved",
            "argv": outcome.argv, "cwd": str(identity.core),
            "effective": {"target": effective.target, "configuration": effective.configuration,
                          "arch": effective.arch, "output_dir": str(effective.output_dir) if effective.output_dir else None,
                          "sources": effective.sources},
            "patches_applied": outcome.prepared_patches,
            "verified_output": outcome.artifact["output_dir"] if outcome.artifact else None,
            "explanation": None if outcome.artifact else
            "Compilation completed but no artifact was verified: %s." % outcome.unresolved_reason}


def unresolved_error(outcome, identity):
    return ScaffoldError(
        "ARTIFACT_UNRESOLVED",
        "Compilation completed but no artifact was verified (%s). Restart, install, and launch were not "
        "attempted." % outcome.unresolved_reason,
        details={"build": build_data(outcome, identity), "phase": "build"}, child_exit_code=0,
        repairs=[repair(["bdev", "run", "--artifact", "<path-to-the-artifact>", "--checkout", str(identity.core)],
                        note="Placeholder path: select the output you built yourself.")])


def finish_build_result(command, outcome, execution, op):
    identity = execution.identity
    result = Result(command=command, operation_id=op.id, child_exit_code=outcome.child_exit,
                    checks=[check.to_dict() for check in execution.checks])
    result.data = {"build": build_data(outcome, identity)}
    if outcome.artifact:
        result.artifacts = [outcome.artifact]
        result.text = "Build completed. Verified output: %s" % outcome.artifact["path"]
    else:
        result.add_warning("ARTIFACT_UNRESOLVED", result.data["build"]["explanation"])
        result.text = result.data["build"]["explanation"]
    return result


# --- command handlers ------------------------------------------------------------------


def plan_result(command, effective, steps):
    """A plan document: every step described the same way, plus the effective choices."""
    result = Result(command=command)
    result.data = {"plan": {"steps": [step.to_dict() for step in steps], "effective": {
        "target": effective.target, "configuration": effective.configuration, "arch": effective.arch,
        "output_dir": str(effective.output_dir) if effective.output_dir else None, "sources": effective.sources}}}
    result.text = step_module.render_plan(command, steps)
    return result


def common_plan_steps(ctx, identity, target, phase, remote):
    """Environment, tools, and readiness as far as they can be judged without loading the environment."""
    try:
        env_module.require_environment(identity, ctx.environ, ctx.log)
        error = None
    except ScaffoldError as caught:
        error = caught
    toolchain, tool_checks = tools_module.inspect_toolchain(identity, ctx.log)
    checks = readiness_checks(ctx, target, phase, remote)
    note = "Judged with the calling environment; execution uses the approved one."
    return toolchain, [step_module.environment_step(identity, error), step_module.tools_step(identity, toolchain, tool_checks),
                       step_module.readiness_step(checks, note)]


def build_plan_steps(ctx, identity, effective, subcommand="build", script_args=(), sync_args=None, run_after=False,
                     device_choice=None):
    """Plan an operation: the same step descriptions execution records, with unresolved parts reported."""
    remote = not effective.offline
    toolchain, steps = common_plan_steps(ctx, identity, effective.target, "sync" if sync_args is not None else "build",
                                         remote)

    def package(arguments):
        return tools_module.package_argv(toolchain, arguments) if toolchain is not None else None

    last = "readiness"
    if sync_args is not None:
        steps.append(step_module.sync_step(identity, sync_args, package(sync_args), ["readiness"]))
        steps.append(step_module.Step(
            "readiness-after-sync", "Check tools and build readiness again once the sync has changed the checkout.",
            "unresolved", needs=["sync"], on_failure="The build does not start.",
            detail="Judged after the sync ran; it cannot be known before."))
        last = "readiness-after-sync"
    try:
        patch_plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root)
    except ScaffoldError as error:
        patch_plan = error
    patch_step = step_module.patches_step(identity, patch_plan, package(["run", "apply_patches"]),
                                          after_sync=sync_args is not None)
    patch_step.needs = [last]
    steps.append(patch_step)
    last = "patch-preparation"
    changes_files = sync_args is not None or getattr(patch_plan, "action", None) == "apply"
    android = effective.target == "android"
    if android:
        last, refreshes = plan_android_preparation(ctx, identity, effective, steps)
        changes_files = changes_files or refreshes
    explicit = subcommand == "build" and bool(ctx.parsed.get("force_gn"))
    arguments = build_arguments(effective, subcommand, script_args, explicit)
    conditional = [FORCE_GN_ARGUMENT] if subcommand == "build" and changes_files and not explicit else []
    needs = [last, *(["sync"] if sync_args is not None else [])]
    steps.append(step_module.build_step(identity, effective, subcommand, arguments, package(arguments), needs,
                                        conditional))
    if subcommand == "build":
        steps.append(step_module.verify_step(effective, [subcommand]))
    if run_after:
        steps.extend(plan_restart_after_build(ctx, effective, android, device_choice))
    return steps


def plan_android_preparation(ctx, identity, effective, steps):
    try:
        support_plan = android_deps.plan_preparation(ctx, identity, ctx.log)
        wc = android_deps.working_copy(identity)
        writes = [str(identity.src / key) for key in android_deps.planned_writes(identity, wc, support_plan.scripts, ctx.log)] \
            if support_plan.action == "refresh" else []
    except ScaffoldError as error:
        support_plan, writes = error, []
    steps.append(step_module.support_step(identity, support_plan, writes))
    args_gn = effective.output_dir / "args.gn"
    steps.append(step_module.gn_step(effective, args_gn, effective.chosen_gn_keys))
    return "gn-overrides", getattr(support_plan, "action", None) == "refresh"


def plan_restart_after_build(ctx, effective, android, device_choice):
    if android:
        try:
            _, device, source = _android().preflight_device(ctx)
            chosen = step_module.select_device_step({"id": device["id"], "source": source})
        except ScaffoldError as error:
            return [step_module.select_device_step(error=error)]
        apk = str(_android().apk_path(effective.output_dir, effective.arch))
        package = "<package from the built APK>"
        return [chosen, step_module.install_apk_step(device["id"], apk),
                step_module.stop_package_step(device["id"], package),
                step_module.launch_package_step(device["id"], package)]
    bundle = str(macos.app_path(effective.output_dir, effective.configuration, effective.channel))
    return [step_module.stop_instances_step(None, [], ["verify-output"]),
            step_module.launch_step(bundle, True, ["stop-running-instances"])]


def do_build(ctx, command, sync_first=False, run_after=False):
    parsed = ctx.parsed
    target_token = parsed.positionals[0] if parsed.positionals else None
    identity, effective = select_build(ctx, target_token, parsed.forwarded)
    if parsed.get("plan"):
        sync_plan = sync_arguments(ctx, effective.target, [], identity) if sync_first else None
        return plan_result(command, effective, build_plan_steps(ctx, identity, effective, "build", (), sync_plan,
                                                                run_after))
    if parsed.get("device") and effective.target != "android":
        raise ScaffoldError("INVALID_INPUT", "--device applies to Android only.")
    execution = execution_module.load(ctx, identity)
    device = _android().preflight_device(execution.context(ctx)) if run_after and effective.target == "android" \
        else None
    remote = not effective.offline
    execution = prepare(ctx, identity, effective.target, "sync" if sync_first else "build", remote, execution)
    with track(ctx, command, identity, {"target": effective.target, "configuration": effective.configuration,
                                        "arch": effective.arch}, validated=True) as op:
        sync_result = None
        if sync_first:
            sync_result = do_sync_phase(ctx, execution, op, effective.target, [])
            execution = prepare_for_build(execution, ctx, effective.target, remote)
        outcome = perform_build(ctx, execution, effective, op, bool(parsed.get("force_gn")))
        if run_after and outcome.artifact is None:
            raise unresolved_error(outcome, identity)
        result = finish_build_result(command, outcome, execution, op)
        if sync_result:
            result.data["sync"] = sync_result
        if run_after:
            result = run_phase(ctx, execution, outcome.artifact, result, device, op)
        return op.complete(result)


def cmd_build(ctx):
    return do_build(ctx, "build")


def cmd_build_run(ctx):
    return do_build(ctx, "build-run", run_after=True)


def cmd_sync_build(ctx):
    return do_build(ctx, "sync-build", sync_first=True)


def cmd_sync_build_run(ctx):
    return do_build(ctx, "sync-build-run", sync_first=True, run_after=True)


# --- test ---------------------------------------------------------------------------


def post_parse_test(spec, parsed):
    """`test [<target>] <suite>`: a leading recognized platform name is the target."""
    from ..common.cli import _input_error
    positionals = list(parsed.positionals)
    target = None
    if positionals and positionals[0].lower() in RECOGNIZED_TARGETS:
        target = positionals.pop(0)
    if not positionals:
        raise _input_error("Missing required test suite.", spec,
                           example="bdev test brave_browser_tests --filter 'Example.*'")
    suite = positionals.pop(0)
    if not SUITE_PATTERN.fullmatch(suite):
        raise _input_error("%r is not a test suite name." % suite, spec, common_suites=list(COMMON_SUITES),
                           example="bdev test brave_browser_tests --filter 'Example.*'")
    parsed.values["target"], parsed.values["suite"] = target, suite
    parsed.positionals = []
    parsed.forwarded = positionals + parsed.forwarded


def require_mac_tests(target, source, suite):
    """Android tests are deferred; the effective target decides, wherever it was chosen."""
    if target != "mac":
        raise ScaffoldError("UNSUPPORTED_CAPABILITY",
                            "Android tests are not available in this release; nothing was prepared or built.",
                            details={"target": target, "target_source": source},
                            repairs=[repair(["bdev", "test", "mac", suite], note="Run the suite on macOS instead.")])


def cmd_test(ctx):
    parsed = ctx.parsed
    if buildopts.interpret(parsed.forwarded).target_os is None:
        require_mac_tests(effective_target(parsed.get("target"), ctx.config)[0], "scaffold", parsed.get("suite"))
    identity, effective = select_build(ctx, parsed.get("target"), parsed.forwarded)
    require_mac_tests(effective.target, effective.sources["target"], parsed.get("suite"))
    script_args = [parsed.get("suite")]
    if parsed.get("filter"):
        script_args.append("--filter=%s" % parsed.get("filter"))
    if parsed.get("plan"):
        return plan_result("test", effective, build_plan_steps(ctx, identity, effective, "test", script_args))
    execution = prepare(ctx, identity, effective.target, "build", not effective.offline)
    with track(ctx, "test", identity, {"target": effective.target, "suite": parsed.get("suite")}, validated=True) as op:
        arguments = build_arguments(effective, "test", script_args, False)
        outcome = run_test_package(ctx, execution, effective, op, arguments)
        result = Result(command="test", child_exit_code=0, checks=[check.to_dict() for check in execution.checks])
        result.data = {"suite": parsed.get("suite"), "argv": outcome, "cwd": str(identity.core)}
        result.text = "Test suite %s passed." % parsed.get("suite")
        return op.complete(result)


def run_test_package(ctx, execution, effective, op, arguments):
    prepare_patches(ctx, execution, op)
    argv, state = run_output_step(ctx, execution, effective, op, arguments, "test",
                                  metal_environment(ctx, execution.environ) if effective.target == "mac" else {})
    state.end_attempt_completed(op.id)
    return argv


# --- run --------------------------------------------------------------------------------


def run_candidates(ctx, identity, target, configuration, arch):
    """Valid application bundles for this checkout, target, configuration, and architecture."""
    found = {}
    default = identity.src / "out" / buildopts.default_build_dir(target, configuration, arch)
    paths = [macos.app_path(default, configuration)]
    for state in output_states(identity, ctx.state_root):
        success = state.get("success") or {}
        artifact = success.get("artifact") or {}
        if artifact.get("target") == target and artifact.get("configuration") == configuration \
                and artifact.get("arch") == arch:
            paths.append(Path(artifact["path"]))
    for path in paths:
        try:
            bundle = macos.read_bundle(path)
        except ScaffoldError:
            continue
        found[os.path.realpath(path)] = bundle
    return list(found.values())


def select_artifact(ctx, identity, target, configuration, arch):
    explicit = ctx.parsed.get("artifact")
    if explicit:
        path = Path(os.path.expanduser(explicit))
        path = path if path.is_absolute() else Path(ctx.cwd) / path
        return macos.read_bundle(Path(os.path.normpath(path)))
    candidates = run_candidates(ctx, identity, target, configuration, arch)
    if not candidates:
        raise ScaffoldError(
            "ARTIFACT_MISSING", "No %s %s %s application was found for this checkout." % (
                target, configuration, arch),
            details={"searched": str(identity.src / "out" / buildopts.default_build_dir(target, configuration, arch))},
            repairs=[repair(["bdev", "build", target, "--checkout", str(identity.core)]),
                     repair(["bdev", "build-run", target, "--checkout", str(identity.core)])])
    if len(candidates) > 1:
        raise ScaffoldError(
            "ARTIFACT_AMBIGUOUS", "More than one application matches; choose one with --artifact.",
            details={"candidates": [item["path"] for item in candidates]},
            repairs=[repair(["bdev", "run", "--artifact", candidates[0]["path"]],
                            note="Example only; choose the application you intend to run.")])
    return candidates[0]


def artifact_freshness(ctx, identity, output_dir, android=False):
    state = OutputState(identity, output_dir, ctx.state_root)
    report = patches.collect_drift(identity)
    recorded = (state.success or {}).get("fingerprint")
    current = freshness.compute(identity, report.patched_paths, [], ctx.log,
                                android_deps.freshness_inputs(identity, ctx.log) if android else None)
    return freshness.assess(recorded, current, state)


def add_freshness_warning(result, assessment):
    if assessment["status"] == "stale":
        result.add_warning("STALE_BUILD", "This output does not include the latest changes: %s" %
                           "; ".join(assessment["evidence"]), freshness=assessment)
    elif assessment["status"] == "unknown":
        result.add_warning("UNKNOWN_FRESHNESS", freshness.UNKNOWN_MESSAGE, freshness=assessment)


def run_phase(ctx, execution, bundle, result, device=None, op=None):
    """Restart the browser (or reinstall the APK) with a validated output and add the outcome to a result."""
    identity = execution.identity
    ctx = execution.context(ctx)
    if op is not None:
        op.step("run", artifact=bundle["path"])
        if bundle.get("kind") != "apk":
            described = step_module.launch_step(bundle["path"], True)
            op.step(described.name, **described.record())
    if bundle.get("kind") == "apk":
        return _android().restart_apk(ctx, identity, bundle, result, device, op)
    assessment = artifact_freshness(ctx, identity, Path(bundle["path"]).parent)
    add_freshness_warning(result, assessment)
    outcome = macos.restart(bundle, ctx.environ, ctx.log)
    result.data = {**(result.data or {}), "run": {"artifact": bundle, "freshness": assessment, **outcome}}
    if not result.artifacts:
        result.artifacts = [{**bundle, "verified": False, "freshness": assessment["status"]}]
    result.text = ((result.text + "\n") if result.text else "") + "Restarted %s (%s)." % (
        bundle["name"], bundle["path"])
    return result


def environment_plan_step(ctx, identity):
    try:
        env_module.require_environment(identity, ctx.environ, ctx.log)
        return step_module.environment_step(identity)
    except ScaffoldError as error:
        return step_module.environment_step(identity, error)


def run_plan(ctx, identity, bundle):
    """The plan for restarting the selected application: what it would stop and launch, changing nothing."""
    running = [item["pid"] for item in macos.running_instances(bundle, ctx.environ, ctx.log)]
    selected = step_module.Step("select-artifact", "Use the selected application; run never builds.", "resolved",
                                reads=[bundle["path"]], detail=bundle["path"], needs=["environment"])
    steps = [environment_plan_step(ctx, identity), selected,
             step_module.stop_instances_step(bundle["bundle_identifier"], running, ["select-artifact"]),
             step_module.launch_step(bundle["path"], False, ["stop-running-instances"])]
    result = Result(command="run", data={"plan": {"artifact": bundle, "steps": [step.to_dict() for step in steps]}})
    result.text = step_module.render_plan("run", steps)
    return result


def cmd_deploy(ctx):
    """`deploy android` is `run android`."""
    if ctx.parsed.positionals[0].lower() != "android":
        raise ScaffoldError("INVALID_INPUT", "deploy installs an Android build; use 'bdev run' for macOS.",
                            details={"example": "bdev deploy android"})
    return cmd_run(ctx)


def cmd_run(ctx):
    parsed = ctx.parsed
    identity = ctx.identity()
    target, _ = effective_target(parsed.positionals[0] if parsed.positionals else None, ctx.config)
    require_available_target(target)
    if parsed.get("device") and target != "android":
        raise ScaffoldError("INVALID_INPUT", "--device applies to Android only.")
    execution = None if parsed.get("plan") else execution_module.load(ctx, identity)
    if target == "android":
        return _android().run_android(execution.context(ctx) if execution else ctx, identity, execution is not None)
    configuration = requested_configuration(ctx) or "Debug"
    bundle = select_artifact(ctx, identity, target, configuration, "arm64")
    if parsed.get("plan"):
        return run_plan(ctx, identity, bundle)
    with track(ctx, ctx.command, identity, {"target": target, "artifact": bundle["path"]}, validated=True) as op:
        return op.complete(run_phase(ctx, execution, bundle, Result(command=ctx.command), None, op))


# --- sync ------------------------------------------------------------------------------


def gclient_targets(identity):
    """Existing target_os values from the checkout's .gclient (literals only; nothing is executed)."""
    import ast
    path = identity.workspace / ".gclient"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "target_os" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
            found = [value] if isinstance(value, str) else list(value)
    return found


def target_os_union(existing, requested):
    unrelated = [item for item in existing if item not in ("android", "ios", "mac", "macos", "desktop")]
    mobile = {item for item in existing if item in ("android", "ios")} | set(requested)
    return unrelated + [item for item in ("android", "ios") if item in mobile]


def sync_arguments(ctx, target, forwarded, identity=None):
    arguments = ["run", "sync"]
    if target == "android":
        identity = identity or ctx.identity()
        existing = gclient_targets(identity)
        if existing is None:
            raise ScaffoldError("PREPARATION_CONFLICT", "The checkout's .gclient is missing or unreadable.",
                                details={"file": str(identity.workspace / ".gclient")})
        arguments.append("--target_os=" + ",".join(target_os_union(existing, ["android"])))
    return [*arguments, *forwarded]


def local_work_conflicts(ctx, identity, adopt=False):
    """Evidence of local work that a source sync could reset or overwrite.

    Covers every repository the sync can reset, and the files applying patches afterwards would replace.
    Changes that are the recorded output of patch or support preparation are not local work.
    """
    plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root)
    report = plan.report
    expected = {identity.src / path for path in report.patched_paths if path not in report.files}
    expected |= android_deps.recorded_results(identity, ctx.state_root)
    expected |= patches.core_written_paths(identity, ctx.state_root)
    conflicts = sync_scope.local_work(identity, sync_scope.sync_repositories(identity), expected,
                                      sync_scope.read_baseline(identity, ctx.state_root), ctx.state_root, ctx.log,
                                      adopt)
    if plan.action == "conflict":
        conflicts.extend(plan.conflicts)
    return conflicts


def do_sync_phase(ctx, execution, op, target, forwarded):
    identity = execution.identity
    conflicts = local_work_conflicts(ctx, identity, bool(ctx.parsed.get("adopt_local_changes")))
    if conflicts:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Sync could overwrite local work in %d place(s); nothing was changed." % len(conflicts),
                            details={"files": conflicts[:50]},
                            repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)])])
    before = {"core_head": freshness.resolve_head(identity.core, ctx.log),
              "chromium_head": freshness.resolve_head(identity.src, ctx.log)}
    arguments = sync_arguments(ctx, target, forwarded, identity)
    op.step("sync", arguments=arguments, before=before,
            **step_module.sync_step(identity, arguments, tools_module.package_argv(execution.toolchain, arguments)).record())
    argv, code = packages.run(ctx, execution, arguments)
    if code != 0:
        raise ScaffoldError("CHILD_FAILED", "The sync command exited with status %d." % code,
                            details={"argv": argv, "phase": "sync"}, child_exit_code=code)
    after = {"core_head": freshness.resolve_head(identity.core, ctx.log),
             "chromium_head": freshness.resolve_head(identity.src, ctx.log)}
    op.step("sync-complete", after=after)
    try:
        sync_scope.checkpoint(identity, ctx.state_root, ctx.log)
    except ScaffoldError as error:
        op.step("sync-checkpoint", outcome="not recorded", reason=error.message)
    return {"argv": argv, "revisions_before": before, "revisions_after": after}


def cmd_sync(ctx):
    parsed = ctx.parsed
    tokens = [item.strip() for item in (parsed.positionals[0].split(",") if parsed.positionals else [])]
    targets = []
    for token in tokens or [None]:
        target, _ = effective_target(token, ctx.config) if token != "" else (None, None)
        if target is None:
            raise ScaffoldError("INVALID_INPUT", "The target list has an empty item.")
        if target not in targets:
            targets.append(target)
    for target in targets:
        require_available_target(target)
    if any(item.startswith("--target_os") for item in parsed.forwarded) and "android" in targets:
        raise ScaffoldError("SELECTOR_CONFLICT",
                            "Mobile sync targets build --target_os from the checkout's existing targets; "
                            "remove --target_os from the forwarded arguments.")
    identity = ctx.identity()
    mobile = "android" if "android" in targets else "mac"
    if parsed.get("plan"):
        arguments = sync_arguments(ctx, mobile, parsed.forwarded, identity)
        toolchain, steps = common_plan_steps(ctx, identity, "mac", "sync", False)
        steps.append(step_module.sync_step(identity, arguments, tools_module.package_argv(toolchain, arguments)
                                           if toolchain is not None else None, ["readiness"]))
        result = Result(command="sync")
        result.data = {"plan": {"argv_arguments": arguments, "cwd": str(identity.core),
                                "writes": ["source tree and dependencies"], "steps": [step.to_dict() for step in steps]}}
        result.text = step_module.render_plan("sync", steps)
        return result
    execution = prepare(ctx, identity, "mac", "sync")
    with track(ctx, "sync", identity, {"targets": targets}, validated=True) as op:
        phase = do_sync_phase(ctx, execution, op, mobile, parsed.forwarded)
        result = Result(command="sync", child_exit_code=0, data={"sync": phase, "targets": targets},
                        checks=[check.to_dict() for check in execution.checks])
        result.text = "Sync completed for %s." % ", ".join(targets)
        return op.complete(result)


# --- drift and patch update --------------------------------------------------------------


def cmd_drift(ctx):
    identity = ctx.identity()
    report = patches.collect_drift(identity)
    if ctx.parsed.get("diff") or report.files:
        patches.add_numstats(identity, report, ctx.log)
    files = [entry.to_dict() for _, entry in sorted(report.files.items())]
    result = Result(command="drift", data={"drifted": files, "metadata_complete": report.complete,
                                           "incomplete_reasons": report.incomplete,
                                           "patchinfo_files": report.patchinfo_count})
    lines = ["Checked %d patch metadata file(s); %d drifted file(s)." % (report.patchinfo_count, len(files))]
    for entry, drifted in zip(files, (item for _, item in sorted(report.files.items()))):
        lines.append("  %s (%s) %s" % (entry["path"], ", ".join(entry["reasons"]), entry["numstat"]))
        if ctx.parsed.get("diff"):
            repository = drifted.repository or identity.src
            diff = run_capture(["git", "-C", str(repository), "diff", "--", drifted.relative or entry["path"]],
                               str(repository), None, ctx.log, timeout=120)
            lines.extend("      " + line for line in (diff.stdout.splitlines() or ["(no git diff)"]))
    if not report.complete:
        lines.append("Evidence is incomplete, so this is not a clean result:")
        lines.extend("  - " + reason for reason in report.incomplete)
        result.add_warning("INCOMPLETE_EVIDENCE", "; ".join(report.incomplete))
    elif not files:
        lines.append("All patched files match their metadata.")
    result.text = "\n".join(lines)
    return result


def cmd_patches_update(ctx):
    identity = ctx.identity()
    execution = prepare(ctx, identity, "mac")
    with track(ctx, "patches update", identity, {}, validated=True) as op:
        argv, code = packages.run(ctx, execution, ["run", "update_patches", *ctx.parsed.forwarded])
        if code != 0:
            raise ScaffoldError("CHILD_FAILED", "update_patches exited with status %d." % code,
                                details={"argv": argv}, child_exit_code=code)
        status = run_capture(["git", "-C", str(identity.core), "status", "--short", "--branch"], str(identity.core),
                             None, ctx.log, timeout=120)
        changes = [line for line in status.stdout.splitlines()[1:]]
        result = Result(command="patches update", child_exit_code=0, data={"argv": argv, "changed_files": changes})
        result.text = ("Patch changes for review (nothing was committed):\n  " + "\n  ".join(changes)) if changes \
            else "No patch changes detected."
        return op.complete(result)
