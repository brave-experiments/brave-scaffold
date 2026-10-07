# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Sync, build, test, run, and their combinations."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..common import env as env_module
from ..common import tools as tools_module
from ..common.checks import readiness_error
from ..common.platforms import RECOGNIZED_TARGETS, effective_target, normalize_target
from ..common.procs import run_capture
from ..common.results import Cancelled, Result, ScaffoldError, repair
from . import (android, android_deps, android_tests, buildopts, execution as execution_module, freshness, macos,
               packages, patches, steps as step_module, sync as sync_module)
from .records import OutputState, output_states, revalidation_warning, track

SUITE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*_tests?")
COMMON_SUITES = ("brave_browser_tests", "brave_unit_tests", "brave_all_unit_tests", "chromium_unit_tests",
                 "browser_tests")


# --- selection and gates -------------------------------------------------------------


def requested_configuration(ctx):
    value = ctx.parsed.get("configuration")
    return value.capitalize() if value else None


def select_build(ctx, target_token, forwarded, tests=False):
    """Resolve identity and the effective build choices; conflicts fail before any effect."""
    identity = ctx.identity()
    target, _ = effective_target(target_token, ctx.config)
    explicit = normalize_target(target_token) if target_token else None
    configuration = requested_configuration(ctx)
    effective = buildopts.resolve_effective(
        identity.src, forwarded, target, configuration or "Debug", explicit, configuration,
        bool(ctx.parsed.get("offline")), tests=tests)
    if effective.target == "ios" and target != "ios" and not tests:
        raise ScaffoldError(
            "SELECTOR_CONFLICT", "--target_os=ios in the forwarded arguments cannot select an iOS build here; "
            "iOS builds run through Xcode, not the package build command.",
            details={"forwarded": "ios", "example": "bcore build ios"})
    require_available_target(effective.target, "test" if tests else "build")
    return identity, effective


def require_available_target(target, operation="build"):
    if target == "android":
        android.require_available()
    elif target == "ios":
        if operation == "test":
            raise ScaffoldError("UNSUPPORTED_CAPABILITY", "Tests are not available for iOS.")
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
    if target == "mac" or phase == "sync":
        groups = ("host-mac", "mac-build")
    elif target == "ios":
        groups = ("host-mac", "ios-machine", "ios-build")
    else:
        groups = ("android-build",)
    for group in groups:
        function = doctor.GROUP_FUNCTIONS[group]
        scope = {"android-build": "android", "ios-machine": "ios", "ios-build": "ios"}.get(group, "mac")
        if group in ("mac-build", "android-build", "ios-build"):
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
    with ctx.log.measure("Readiness"):
        execution = execution_module.resolve_tools(execution, ctx)
        return execution.with_checks(readiness_gate(execution.context(ctx), target, phase, remote_required))


def prepare_for_build(execution, ctx, target, remote_required):
    """After sync changed the checkout: resolve its tools and judge the build's readiness again."""
    with ctx.log.measure("Readiness"):
        execution = execution_module.resolve_tools(execution, ctx)
        return execution.with_checks(readiness_gate(execution.context(ctx), target, "build", remote_required))


def metal_environment(ctx, environ, checks=()):
    """Point the build at an installed Metal toolchain when `xcrun metal` cannot find one."""
    metal = next((check for check in reversed(checks) if check.name == "metal-toolchain"), None)
    if metal and metal.evidence.get("xcrun_works"):
        return {}
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
    op.note("patch-preparation-plan", action=plan.action, reason=plan.reason,
            **step_module.patches_step(identity, plan, apply_argv).record())
    if plan.action == "current":
        state = patches.snapshot_files(identity, plan.report)
        patches.write_receipt(identity, plan.trees, state, ctx.state_root)
        return False, plan
    if plan.action == "conflict":
        raise patches.conflict_error(plan, identity)
    op.start("apply-patches", **step_module.patches_step(identity, plan, apply_argv).record())
    argv, code = packages.run(ctx, execution, ["run", "apply_patches"])
    if code != 0:
        op.fail("apply-patches", exit=code)
        raise ScaffoldError("CHILD_FAILED", "Applying Core patches failed (exit %d)." % code,
                            details={"argv": argv, "phase": "patches"}, child_exit_code=code)
    after = patches.collect_drift(identity)
    if after.files:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Patches were applied but %d file(s) still differ from their metadata." % len(after.files),
                            details={"files": sorted(after.files)[:50]},
                            repairs=[repair(["bcore", "drift", "--diff", "--checkout", str(identity.core)])])
    patches.write_receipt(identity, plan.trees, patches.snapshot_files(identity, after), ctx.state_root,
                          patches.core_output(identity))
    op.succeed("apply-patches", exit=0)
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
    return android.verify_artifact(effective, identity, environ, log)


# --- build phase ---------------------------------------------------------------------


FORCE_GN_ARGUMENT = "--force_gn_gen"


def build_arguments(effective, subcommand, script_args, force_gn, tail=()):
    """`tail` follows the forwarded arguments: the test command reads everything after an unknown option as unknown."""
    arguments = ["run", subcommand, *script_args, *effective.generated]
    if force_gn:
        arguments.append(FORCE_GN_ARGUMENT)
    return [*arguments, *effective.forwarded, *tail]


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
    op.start(phase, package_arguments=arguments,
             **step_module.build_step(identity, effective, phase, arguments, argv_plan, ["patch-preparation"]).record())
    try:
        if before_child is not None:
            before_child()
        with ctx.log.measure("Package " + phase):
            argv, code = packages.run(ctx, execution, arguments, extra_env)
    except Cancelled:
        if state is not None:
            state.end_attempt(op.id, "cancelled")
        raise
    if code != 0 and state is not None:
        state.end_attempt(op.id, "failed")
    if code != 0:
        op.fail(phase, exit=code)
        raise ScaffoldError("CHILD_FAILED", "The package %s command exited with status %d." % (phase, code),
                            details={"argv": argv, "cwd": str(identity.core), "output_dir": str(effective.output_dir) if effective.output_dir else None},
                            child_exit_code=code)
    op.succeed(phase, exit=0)
    return argv, state


def perform_build(ctx, execution, effective, op, force_gn=False):
    """Prepare sources, run the package build, then verify the resulting output."""
    identity = execution.identity
    platform = "macOS" if effective.target == "mac" else effective.target
    ctx.log.phase("Building Brave for %s (%s, %s)" % (platform, effective.configuration, effective.arch))
    ctx.log.phase("Output directory: %s" % (effective.output_dir or "unresolved"))
    mode = "local (remote execution disabled)" if effective.offline else "online (RBE/Siso requested)"
    ctx.log.phase("Build mode: " + mode)
    with ctx.log.measure("Source preparation"):
        changed, plan = prepare_patches(ctx, execution, op)
        is_android = effective.target == "android"
        if is_android:
            refreshed = android.prepare_support(ctx, execution, op, effective)
            if refreshed:
                patches.record_extra_expected(identity, ctx.state_root)
            changed = refreshed or changed
    arguments = build_arguments(effective, "build", (), force_gn or changed)
    if is_android:
        described = step_module.gn_step(effective, effective.preparation_dir / "args.gn", effective.chosen_gn_keys)
        op.start(described.name, **described.record())
    op.detail(effective={"target": effective.target, "configuration": effective.configuration,
                         "arch": effective.arch, "output_dir": str(effective.output_dir) if effective.output_dir else None,
                         "package_arguments": arguments})

    def write_overrides():
        android.write_gn_overrides(identity, effective)
        op.succeed("gn-overrides")

    argv, state = run_output_step(
        ctx, execution, effective, op, arguments, "build",
        android.build_environment(execution.context(ctx)) if is_android else
        metal_environment(ctx, execution.environ, execution.checks),
        write_overrides if is_android else None)
    op.start("verify-output", output_dir=str(effective.output_dir) if effective.output_dir else None)
    try:
        with ctx.log.measure("Output verification"):
            artifact, reason = artifact_for(effective, identity, execution.environ, ctx.log)
    except ScaffoldError as error:
        op.fail("verify-output", code=error.code)
        if state is not None:
            state.end_attempt(op.id, "output-invalid")
        error.child_exit_code = 0
        error.details.setdefault("argv", argv)
        raise
    op.succeed("verify-output", artifact_status="verified" if artifact else "unresolved",
               verified_output=artifact["output_dir"] if artifact else None, explanation=reason)
    if artifact:
        op.attach_artifacts([artifact])
    with ctx.log.measure("Source state"):
        inputs = freshness.compute(identity, plan.report.patched_paths, arguments, ctx.log,
                                   android_deps.freshness_inputs(identity, ctx.log) if is_android else None)
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
        repairs=[repair(["bcore", "run", "--artifact", "<path-to-the-artifact>", "--checkout", str(identity.core)],
                        note="Placeholder path: select the output you built yourself.")])


def finish_build_result(command, outcome, execution, op):
    identity = execution.identity
    result = Result(command=command, operation_id=op.id, child_exit_code=outcome.child_exit,
                    checks=[check.to_dict() for check in execution.checks])
    result.data = {"build": build_data(outcome, identity)}
    if outcome.artifact:
        result.artifacts = [outcome.artifact]
        result.text = "✅ Build completed.\nVerified output: %s" % outcome.artifact["path"]
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
                     device_choice=None, tail_args=()):
    """Plan an operation: the same step descriptions execution records, with unresolved parts reported."""
    remote = not effective.offline
    toolchain, steps = common_plan_steps(ctx, identity, effective.target, "sync" if sync_args is not None else "build",
                                         remote)

    def package(arguments):
        return tools_module.package_argv(toolchain, arguments) if toolchain is not None else None

    last = "readiness"
    if sync_args is not None:
        steps.append(sync_module.plan_step(identity, sync_args, toolchain, ["readiness"]))
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
    is_android = effective.target == "android"
    if is_android:
        last, refreshes = plan_android_preparation(ctx, identity, effective, steps)
        changes_files = changes_files or refreshes
    explicit = subcommand == "build" and bool(ctx.parsed.get("force_gn"))
    arguments = build_arguments(effective, subcommand, script_args, explicit, tail_args)
    conditional = [FORCE_GN_ARGUMENT] if (subcommand == "build" or is_android) and changes_files and not explicit else []
    needs = [last, *(["sync"] if sync_args is not None else [])]
    steps.append(step_module.build_step(identity, effective, subcommand, arguments, package(arguments), needs,
                                        conditional))
    if subcommand == "build":
        steps.append(step_module.verify_step(effective, [subcommand]))
    if run_after:
        steps.extend(plan_restart_after_build(ctx, effective, is_android, device_choice))
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
    args_gn = effective.preparation_dir / "args.gn"
    steps.append(step_module.gn_step(effective, args_gn, effective.chosen_gn_keys))
    return "gn-overrides", getattr(support_plan, "action", None) == "refresh"


def plan_restart_after_build(ctx, effective, is_android, device_choice):
    if effective.unresolved:
        return [step_module.Step("restart-after-build", "Restart only a verified artifact from this build.",
                                 "unresolved", needs=["verify-output"],
                                 detail="; ".join(effective.unresolved),
                                 on_failure="Nothing is stopped, installed, or launched.")]
    if is_android:
        try:
            choice = android.preflight_deployment(ctx, effective.arch)
        except ScaffoldError as error:
            return [step_module.select_device_step(error=error)]
        apk = str(android.apk_path(effective.output_dir, effective.arch))
        package = "<package from the built APK>"
        return android.deployment_steps(choice, apk, package)
    bundle = str(macos.app_path(effective.output_dir, effective.configuration, effective.channel))
    return [step_module.stop_instances_step(None, [], ["verify-output"]),
            step_module.launch_step(bundle, True, ["stop-running-instances"])]


SYNC_STYLE_C_VALUES = ("true", "false", "1", "0")


def do_build(ctx, command, sync_first=False, run_after=False):
    parsed = ctx.parsed
    target_token = parsed.positionals[0] if parsed.positionals else None
    if parsed.get("sync_arg") and not sync_first:
        raise ScaffoldError("INVALID_INPUT", "--sync-arg applies to sync-build and sync-build-run (sb, sbr) only; "
                            "%s has no sync phase." % command, details={"example": "bcore sb --sync-arg=--force"})
    if parsed.get("all_devices"):
        if not run_after:
            raise ScaffoldError("INVALID_INPUT", "--all-devices applies to Android build-run and sync-build-run only.")
        if parsed.get("device"):
            raise ScaffoldError("SELECTOR_CONFLICT", "Use either --device or --all-devices, not both.")
    if effective_target(target_token, ctx.config)[0] == "ios":
        if parsed.get("all_devices"):
            raise ScaffoldError("INVALID_INPUT", "--all-devices applies to Android only.")
        from . import cmd_ios
        return cmd_ios.do_build(ctx, command, sync_first, run_after)
    identity, effective = select_build(ctx, target_token, parsed.forwarded)
    if sync_first and effective.sources.get("output") == "forwarded" \
            and str(effective.build_dir_arg).lower() in SYNC_STYLE_C_VALUES:
        raise ScaffoldError(
            "INVALID_INPUT", "-C %s would name the build output directory %r here. Core's sync command reads "
            "-C true|false as \"force or skip the Chromium sync\"; to send that to the sync phase, use "
            "--sync-arg=-C --sync-arg=%s." % (effective.build_dir_arg, effective.build_dir_arg, effective.build_dir_arg),
            details={"example": "bcore %s --sync-arg=-C --sync-arg=%s" % (command, effective.build_dir_arg)})
    if parsed.get("all_devices") and effective.target != "android":
        raise ScaffoldError("INVALID_INPUT", "--all-devices applies to Android only.")
    if parsed.get("skip_support_refresh") and effective.target != "android":
        raise ScaffoldError("INVALID_INPUT", "--skip-support-refresh applies to Android only.")
    if parsed.get("device") and effective.target != "android":
        raise ScaffoldError("INVALID_INPUT", "--device applies to Android and iOS only.")
    if parsed.get("plan"):
        sync_plan = (sync_module.sync_arguments(ctx, effective.target, list(parsed.get("sync_arg") or []), identity)
                     if sync_first else None)
        return plan_result(command, effective, build_plan_steps(ctx, identity, effective, "build", (), sync_plan,
                                                                run_after))
    execution = execution_module.load(ctx, identity)
    device = android.preflight_deployment(execution.context(ctx), effective.arch) if run_after and effective.target == "android" \
        else None
    remote = not effective.offline
    execution = prepare(ctx, identity, effective.target, "sync" if sync_first else "build", remote, execution)
    with track(ctx, command, identity, {"target": effective.target, "configuration": effective.configuration,
                                        "arch": effective.arch}, validated=True) as op:
        sync_result = None
        if sync_first:
            sync_result = sync_module.do_sync_phase(ctx, execution, op, effective.target,
                                                    list(parsed.get("sync_arg") or []))
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
    """`test [<target>] [<suite>]`: a leading recognized platform name is the target; no suite selects by changes or file."""
    from ..common.cli import _input_error
    positionals = list(parsed.positionals)
    target = None
    if positionals and positionals[0].lower() in RECOGNIZED_TARGETS:
        target = positionals.pop(0)
    suite = None
    if positionals:
        suite = positionals.pop(0)
        if not SUITE_PATTERN.fullmatch(suite):
            raise _input_error("%r is not a test suite name." % suite, spec, common_suites=list(COMMON_SUITES),
                               example="bcore test brave_browser_tests --filter 'Example.*'")
    selectors = [name for name, given in (("a test suite", suite), ("--file", parsed.get("file")),
                                          ("--base", parsed.get("base"))) if given]
    if suite and len(selectors) > 1 or parsed.get("file") and parsed.get("base"):
        raise _input_error("%s cannot be combined: choose a suite, --file, or changed-test discovery." % (
            " and ".join(selectors).capitalize()), spec, example="bcore test --file components/example/example_unittest.cc")
    if parsed.get("filter") and not suite:
        raise _input_error("--filter narrows a named suite; name one, or let discovery build the filters.", spec,
                           example="bcore test brave_unit_tests --filter 'Example.*'")
    parsed.values["target"], parsed.values["suite"] = target, suite
    parsed.positionals = []
    parsed.forwarded = positionals + parsed.forwarded


def log_test_phase(ctx, effective):
    ctx.log.phase("Building and running test suite %s (%s, %s, %s)" % (
        ctx.parsed.get("suite"), effective.target, effective.configuration, effective.arch))
    ctx.log.phase("Output directory: %s" % (effective.output_dir or "unresolved"))
    ctx.log.phase("Build mode: " + ("local (remote execution disabled)" if effective.offline
                                   else "online (RBE/Siso requested)"))


def cmd_test(ctx):
    parsed = ctx.parsed
    identity, effective = select_build(ctx, parsed.get("target"), parsed.forwarded, tests=True)
    if effective.target == "android":
        return cmd_android_test(ctx, identity, effective)
    if parsed.get("all_devices"):
        raise ScaffoldError("INVALID_INPUT", "--all-devices applies to device-backed Android tests only.")
    if parsed.get("device"):
        raise ScaffoldError("INVALID_INPUT", "--device applies to Android only.")
    script_args = [parsed.get("suite")]
    if parsed.get("filter"):
        script_args.append("--filter=%s" % parsed.get("filter"))
    results = desktop_results_path(effective, parsed.forwarded)
    tail = ["%s=%s" % (SUMMARY_OPTION, results)] if results else []
    if parsed.get("plan"):
        return plan_result("test", effective, build_plan_steps(ctx, identity, effective, "test", script_args,
                                                               tail_args=tail))
    execution = prepare(ctx, identity, effective.target, "build", not effective.offline)
    with track(ctx, "test", identity, {"target": effective.target, "suite": parsed.get("suite")}, validated=True) as op:
        arguments = build_arguments(effective, "test", script_args, False, tail)
        argv, summary, warning = run_test_package(ctx, execution, effective, op, arguments, results)
        result = Result(command="test", child_exit_code=0, checks=[check.to_dict() for check in execution.checks])
        result.data = {"suite": parsed.get("suite"), "argv": argv, "cwd": str(identity.core), "results": summary}
        result.text = "Test suite %s passed." % parsed.get("suite")
        if summary:
            result.text = "Test suite %s passed (%d passed, %d skipped)." % (
                parsed.get("suite"), summary["passed"], summary["skipped"])
        if warning:
            result.add_warning("TEST_RESULTS_UNVERIFIED", warning)
        return op.complete(result)


SUMMARY_OPTION = "--test-launcher-summary-output"
DESKTOP_RESULTS_NAME = "scaffold_test_results.json"


def desktop_results_path(effective, forwarded):
    """Where the test launcher writes its JSON summary, or None when the caller chose their own or no output is known."""
    if effective.unresolved or not effective.preparation_dir:
        return None
    if any(token.partition("=")[0] == SUMMARY_OPTION for token in forwarded):
        return None
    return Path(effective.preparation_dir) / DESKTOP_RESULTS_NAME


def verify_desktop_results(results):
    """After a zero exit, trust the launcher's own counts over the exit code. Returns (summary, warning)."""
    summary = android_tests.summarize_results(results)
    if summary is None:
        return None, "The test command exited 0 but wrote no readable summary (%s), so the test count is unverified." % results
    if summary["failed"]:
        raise ScaffoldError("TEST_FAILED", "%d test(s) failed although the test command exited 0." % summary["failed"],
                            details={"results": summary}, child_exit_code=0)
    if summary["ran"] == 0:
        raise ScaffoldError(
            "NO_TESTS_RAN", "The test command exited 0 but ran no tests; the filter may match nothing. Parameterized "
            "fixtures (TEST_P) need an instantiation prefix, such as '*/Fixture.*'.",
            details={"results": summary}, child_exit_code=0)
    return summary, None


def finish_test_attempt(state, op, verify):
    """Record the attempt as complete only once its results are verified.

    A run can exit 0 and still fail (failed tests, no tests, no runner). That is a failed attempt: the output stays
    marked for revalidation, because the test command builds into the same directory as the application.
    """
    try:
        outcome = verify()
    except ScaffoldError:
        if state is not None:
            state.end_attempt(op.id, "failed")
        raise
    if state is not None:
        state.end_attempt_completed(op.id)
    return outcome


def run_test_package(ctx, execution, effective, op, arguments, results=None):
    log_test_phase(ctx, effective)
    op.detail(effective={"target": effective.target, "configuration": effective.configuration,
                         "arch": effective.arch, "output_dir": str(effective.output_dir) if effective.output_dir else None,
                         "package_arguments": arguments})
    prepare_patches(ctx, execution, op)

    def before_child():
        if results is not None:
            results.unlink(missing_ok=True)

    try:
        argv, state = run_output_step(ctx, execution, effective, op, arguments, "test",
                                      metal_environment(ctx, execution.environ, execution.checks)
                                      if effective.target == "mac" else {}, before_child)
    except ScaffoldError as error:
        if results is not None and error.code == "CHILD_FAILED":
            error.details["results"] = android_tests.summarize_results(results)
        raise
    summary, warning = finish_test_attempt(
        state, op, (lambda: verify_desktop_results(results)) if results is not None else (lambda: (None, None)))
    return argv, summary, warning


# --- Android tests -----------------------------------------------------------------------


def android_test_arguments(ctx, kind, device):
    """(options the test command parses, options it passes through) for one Android suite."""
    parsed = ctx.parsed
    known = [parsed.get("suite")]
    if parsed.get("filter"):
        known.append("--filter=%s" % parsed.get("filter"))
    tail = []
    if kind == android_tests.DEVICE:
        known.append("--manual_android_test_device")
        tail += android_tests.device_arguments(device[0], device[1]) if device else ["--device", "<device>", "--adb-path", "<adb>"]
    return known, tail


def cmd_android_test(ctx, identity, effective):
    """Host-side JUnit or device-backed Java tests through Core's test command and the support test overlay."""
    parsed = ctx.parsed
    suite = parsed.get("suite")
    kind = android_tests.suite_kind(suite)
    android_tests.check_options(parsed, kind)
    android_tests.require_support_branch(identity, ctx.log)
    results = android_tests.results_path(effective)
    own_results = not android_tests.forwarded_results_file(parsed.forwarded)
    results_tail = ["--json-results-file=%s" % results] if own_results else []
    if parsed.get("plan"):
        return plan_result("test", effective, android_test_plan(ctx, identity, effective, kind, results_tail))
    execution = execution_module.load(ctx, identity)
    device = (parsed.get("device_group") or android.preflight_deployment(execution.context(ctx), effective.arch)) \
        if kind == android_tests.DEVICE else None
    execution = prepare(ctx, identity, "android", "build", not effective.offline, execution)
    if isinstance(device, android.DeviceGroup):
        return run_android_test_devices(ctx, execution, effective, suite, device)
    details = {"target": "android", "suite": suite, "device": device[1]["id"] if device else None}
    with track(ctx, "test", identity, details, validated=True) as op:
        known, tail = android_test_arguments(ctx, kind, device)
        outcome = run_android_test(ctx, execution, effective, op, known, [*tail, *results_tail], suite,
                                   results if own_results else None)
        result = Result(command="test", child_exit_code=0, checks=[check.to_dict() for check in execution.checks])
        argv, summary, warning = outcome
        result.data = {"suite": suite, "argv": argv, "cwd": str(identity.core), "runs_on": kind,
                       "device": device[1]["id"] if device else None, "output_dir": str(effective.output_dir),
                       "results": summary}
        result.text = "Test suite %s passed." % suite
        if summary:
            result.text = "Test suite %s passed (%d passed, %d skipped)." % (suite, summary["passed"], summary["skipped"])
        if warning:
            result.add_warning("TEST_RESULTS_UNVERIFIED", warning)
        return op.complete(result)


def read_device_runs(report):
    """Runs from the multi-device report; a missing, partial, or malformed report has none."""
    try:
        runs = json.loads(Path(report).read_text())["runs"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return runs if isinstance(runs, list) else []


def run_android_test_devices(ctx, execution, effective, suite, group):
    identity = execution.identity
    if effective.unresolved:
        raise ScaffoldError("ARTIFACT_UNRESOLVED", "Cannot run tests on all devices with unresolved build output.",
                            details={"reasons": effective.unresolved})
    base_results = android_tests.results_option(ctx.parsed.forwarded)
    devices = [{"id": device["id"], "results": str(android_tests.device_results_path(effective, device["id"], base_results))}
               for device in group.devices]
    with track(ctx, "test", identity, {"target": "android", "suite": suite, "devices": devices}, validated=True) as op, \
            tempfile.TemporaryDirectory(prefix="android-test-devices-") as directory:
        config_path, report = Path(directory) / "config.json", Path(directory) / "report.json"
        config_path.write_text(json.dumps({"script": str(identity.core / "build/commands/scripts/test.ts"),
                                          "util": str(identity.core / "build/commands/lib/util.js"),
                                          "runner": str(android_tests.runner_path(effective, suite)),
                                          "devices": devices, "report": str(report)}))
        hook = Path(__file__).with_name("android_test_devices.mjs").as_uri()
        options = (execution.environ.get("NODE_OPTIONS", "") + " --import=" + hook).strip()
        known, tail = android_test_arguments(ctx, android_tests.DEVICE, (group.adapter, group.devices[0]))
        argv, _, _ = run_android_test(ctx, execution, effective, op, known, tail, suite, None,
                                     {"NODE_OPTIONS": options, "SCAFFOLD_ANDROID_TEST_DEVICES": str(config_path)}, report)
        runs = read_device_runs(report)
        if len(runs) != len(devices) or any(run["status"] != "finished" for run in runs):
            raise ScaffoldError("TEST_RESULTS_UNVERIFIED", "The test command did not report a completed run for every selected device.",
                                details={"devices": runs}, exit_code=5)
        outcomes = list(group.skipped)
        warnings = []
        for run in runs:
            summary = android_tests.summarize_results(Path(run["results"]))
            failure = None
            warning = None
            if run["exit"] != 0:
                failure = ScaffoldError("CHILD_FAILED", "The test runner exited with status %s." % run["exit"],
                                        child_exit_code=run["exit"])
            else:
                try:
                    summary, warning = android_tests.verify_outcome(effective, suite, Path(run["results"]), True)
                except ScaffoldError as error:
                    failure = error
            outcomes.append({"device": run["device"], "status": "error" if failure else "ok",
                             "argv": run["argv"], "cwd": run["cwd"], "results": summary,
                             "child_exit_code": run["exit"],
                             **({"code": failure.code, "reason": failure.message} if failure else {})})
            op.note("test-device", **outcomes[-1])
            if warning:
                warnings.append({"code": "TEST_RESULTS_UNVERIFIED", "message": warning,
                                 "details": {"device": run["device"]}})
        failures = [run for run in outcomes if run["status"] == "error"]
        result = Result(command="test", checks=[check.to_dict() for check in execution.checks],
                        child_exit_code=failures[0]["child_exit_code"] if failures else 0)
        result.data = {"suite": suite, "argv": argv, "cwd": str(identity.core), "runs_on": "device",
                       "device_selection": "all-devices", "devices": outcomes, "output_dir": str(effective.output_dir)}
        summaries = [run.get("results") for run in outcomes if run["status"] != "skipped"]
        result.data["results"] = {key: sum(summary[key] for summary in summaries)
                                  for key in ("passed", "failed", "skipped", "ran")} if all(summaries) else None
        result.warnings = warnings
        result.text = "\n".join("%s: %s%s" % (run["device"], "passed" if run["status"] == "ok" else run["status"],
                                             " - " + run["reason"] if run.get("reason") else "") for run in outcomes)
        if failures:
            result.status, result.exit_code = "error", 5
            result.error = {"code": "TEST_FAILED", "message": "Tests failed on %d device(s)." % len(failures),
                            "details": {"devices": outcomes}, "repairs": []}
        op.detail(device_results=outcomes)
        return op.complete(result)


def android_test_plan(ctx, identity, effective, kind, results_tail):
    device_step, device = None, None
    group = None
    if kind == android_tests.DEVICE:
        try:
            choice = android.preflight_deployment(ctx, effective.arch)
            if isinstance(choice, android.DeviceGroup):
                group = choice
                adb, chosen, source = group.adapter, group.devices[0], "all-devices"
            else:
                adb, chosen, source = choice
            device = (adb, chosen)
            device_step = step_module.select_device_step({"id": chosen["id"], "source": source})
        except ScaffoldError as error:
            device_step = step_module.select_device_step(error=error)
    known, tail = android_test_arguments(ctx, kind, device)
    steps = build_plan_steps(ctx, identity, effective, "test", known, tail_args=[*tail, *results_tail])
    names = [step.name for step in steps]
    steps.insert(names.index("gn-overrides"), android_tests.overlay_plan_step(ctx, identity))
    steps[names.index("gn-overrides") + 1].needs = [android_tests.OVERLAY_STEP]
    if device_step is not None:
        steps.insert(names.index("readiness") + 1, device_step)
    if group is not None:
        steps.append(step_module.Step("test-devices", "Build once, then run the suite on each compatible device.",
                                      "planned", writes=[str(android_tests.device_results_path(
                                          effective, d["id"], android_tests.results_option(ctx.parsed.forwarded)))
                                                         for d in group.devices],
                                      detail=", ".join(d["id"] for d in group.devices)))
    return steps


def run_android_test(ctx, execution, effective, op, known, tail, suite, results, extra_env=None, report=None):
    identity = execution.identity
    log_test_phase(ctx, effective)
    with ctx.log.measure("Source preparation"):
        changed, _ = prepare_patches(ctx, execution, op)
        refreshed = android.prepare_support(ctx, execution, op, effective)
        if refreshed:
            patches.record_extra_expected(identity, ctx.state_root)
        applied = android_tests.prepare_overlay(ctx, execution, op)
    failure = None
    try:
        return run_android_test_with_overlay(ctx, execution, effective, op, known, tail, suite, results,
                                             extra_env, report, changed or refreshed)
    except (ScaffoldError, Cancelled) as error:
        failure = error
        raise
    finally:
        if applied:
            try:
                android_tests.remove_overlay(ctx, execution, op)
            except ScaffoldError as error:
                if failure is None:
                    raise
                if isinstance(failure, Cancelled):
                    failure.cleanup_incomplete = True
                else:
                    failure.details["overlay_cleanup"] = {"code": error.code, "message": error.message}


def run_android_test_with_overlay(ctx, execution, effective, op, known, tail, suite, results,
                                  extra_env, report, source_changed):
    identity = execution.identity
    arguments = build_arguments(effective, "test", known, source_changed, tail)
    described = step_module.gn_step(effective, effective.preparation_dir / "args.gn", effective.chosen_gn_keys)
    op.start(described.name, **described.record())
    op.detail(effective={"target": effective.target, "configuration": effective.configuration,
                         "arch": effective.arch, "output_dir": str(effective.output_dir) if effective.output_dir else None,
                         "package_arguments": arguments})

    def before_child():
        android.write_gn_overrides(identity, effective)
        op.succeed("gn-overrides")
        if results is not None:
            results.unlink(missing_ok=True)

    try:
        argv, state = run_output_step(ctx, execution, effective, op, arguments, "test",
                                      {**android.build_environment(execution.context(ctx)), **(extra_env or {})}, before_child)
    except ScaffoldError as error:
        if report is not None and error.code == "CHILD_FAILED" and read_device_runs(report):
            return error.details["argv"], None, None
        if error.code == "CHILD_FAILED" and results is not None:
            error.details["results"] = android_tests.summarize_results(results)
        raise
    if report is not None:
        finish_test_attempt(state, op, lambda: (None, None))
        return argv, None, None
    summary, warning = finish_test_attempt(
        state, op, lambda: android_tests.verify_outcome(effective, suite, results, results is not None))
    return argv, summary, warning


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
            repairs=[repair(["bcore", "build", target, "--checkout", str(identity.core)]),
                     repair(["bcore", "build-run", target, "--checkout", str(identity.core)])])
    if len(candidates) > 1:
        raise ScaffoldError(
            "ARTIFACT_AMBIGUOUS", "More than one application matches; choose one with --artifact.",
            details={"candidates": [item["path"] for item in candidates]},
            repairs=[repair(["bcore", "run", "--artifact", candidates[0]["path"]],
                            note="Example only; choose the application you intend to run.")])
    return candidates[0]


def run_phase(ctx, execution, bundle, result, device=None, op=None):
    """Restart the browser (or reinstall the APK) with a validated output and add the outcome to a result."""
    identity = execution.identity
    ctx = execution.context(ctx)
    if op is not None:
        op.start("run", artifact=bundle["path"])
        if bundle.get("kind") != "apk":
            described = step_module.launch_step(bundle["path"], True)
            op.start(described.name, **described.record())
    if bundle.get("kind") == "apk":
        return android.restart_apk(ctx, identity, bundle, result, device, op)
    outcome = macos.restart(bundle, ctx.environ, ctx.log)
    if op is not None:
        op.succeed("launch", pid=outcome["launched_pid"], stopped=outcome["stopped"])
        op.succeed("run", artifact=bundle["path"])
    result.data = {**(result.data or {}), "run": {"artifact": bundle, **outcome}}
    if not result.artifacts:
        result.artifacts = [{**bundle, "verified": False}]
    result.text = ((result.text + "\n") if result.text else "") + "Restarted %s (%s)." % (
        bundle["name"], bundle["path"])
    return result


def run_plan(ctx, identity, bundle):
    """The plan for restarting the selected application: what it would stop and launch, changing nothing."""
    running = [item["pid"] for item in macos.running_instances(bundle, ctx.environ, ctx.log)]
    selected = step_module.Step("select-artifact", "Use the selected application; run never builds.", "resolved",
                                reads=[bundle["path"]], detail=bundle["path"], needs=["environment"])
    steps = [step_module.plan_environment_step(ctx, identity), selected,
             step_module.stop_instances_step(bundle["bundle_identifier"], running, ["select-artifact"]),
             step_module.launch_step(bundle["path"], False, ["stop-running-instances"])]
    result = Result(command="run", data={"plan": {"artifact": bundle, "steps": [step.to_dict() for step in steps]}})
    result.text = step_module.render_plan("run", steps)
    return result


def cmd_deploy(ctx):
    """`deploy android` is `run android`."""
    if ctx.parsed.positionals[0].lower() != "android":
        raise ScaffoldError("INVALID_INPUT", "deploy installs an Android build; use 'bcore run' for macOS.",
                            details={"example": "bcore deploy android"})
    return cmd_run(ctx)


def warn_unvalidated_output(ctx, identity, bundle, result):
    """Launching never inspects sources, but an interrupted or failed rebuild is recorded and worth saying."""
    message = revalidation_warning(identity, Path(bundle["path"]).parent, bundle["path"], ctx.state_root)
    if message:
        result.add_warning("ARTIFACT_FRESHNESS_UNKNOWN", message)
        ctx.log.phase("Warning [ARTIFACT_FRESHNESS_UNKNOWN]: " + message)


def cmd_run(ctx):
    parsed = ctx.parsed
    identity = ctx.identity()
    target, _ = effective_target(parsed.positionals[0] if parsed.positionals else None, ctx.config)
    if parsed.get("all_devices"):
        if target != "android":
            raise ScaffoldError("INVALID_INPUT", "--all-devices applies to Android only.")
        if parsed.get("device"):
            raise ScaffoldError("SELECTOR_CONFLICT", "Use either --device or --all-devices, not both.")
    require_available_target(target, "run")
    if parsed.get("device") and target not in ("android", "ios"):
        raise ScaffoldError("INVALID_INPUT", "--device applies to Android and iOS only.")
    if target == "ios":
        from . import cmd_ios
        return cmd_ios.cmd_run(ctx, identity)
    execution = None if parsed.get("plan") else execution_module.load(ctx, identity)
    if target == "android":
        return android.run_android(execution.context(ctx) if execution else ctx, identity, execution is not None)
    configuration = requested_configuration(ctx) or "Debug"
    bundle = select_artifact(ctx, identity, target, configuration, "arm64")
    if parsed.get("plan"):
        return run_plan(ctx, identity, bundle)
    result = Result(command=ctx.command)
    warn_unvalidated_output(ctx, identity, bundle, result)
    with track(ctx, ctx.command, identity, {"target": target, "artifact": bundle["path"]}, validated=True) as op:
        return op.complete(run_phase(ctx, execution, bundle, result, None, op))


# --- sync ------------------------------------------------------------------------------


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
        require_available_target(target, "sync")
    if any(item.startswith("--target_os") for item in parsed.forwarded) and sync_module.mobile_targets(targets):
        raise ScaffoldError("SELECTOR_CONFLICT",
                            "Mobile sync targets build --target_os from the checkout's existing targets; "
                            "remove --target_os from the forwarded arguments.")
    identity = ctx.identity()
    mobile = sync_module.mobile_targets(targets) or "mac"
    if parsed.get("plan"):
        arguments = sync_module.sync_arguments(ctx, mobile, parsed.forwarded, identity)
        toolchain, steps = common_plan_steps(ctx, identity, "mac", "sync", False)
        steps.append(sync_module.plan_step(identity, arguments, toolchain, ["readiness"]))
        result = Result(command="sync")
        result.data = {"plan": {"argv_arguments": arguments, "cwd": str(identity.core),
                                "writes": ["source tree and dependencies"], "steps": [step.to_dict() for step in steps]}}
        result.text = step_module.render_plan("sync", steps)
        return result
    execution = prepare(ctx, identity, "mac", "sync")
    with track(ctx, "sync", identity, {"targets": targets}, validated=True) as op:
        phase = sync_module.do_sync_phase(ctx, execution, op, mobile, parsed.forwarded)
        result = Result(command="sync", child_exit_code=0, data={"sync": phase, "targets": targets},
                        checks=[check.to_dict() for check in execution.checks])
        result.text = "Sync completed for %s." % ", ".join(targets)
        return op.complete(result)
