# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Sync, build, test, run, and their combinations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..common import env as env_module
from ..common import tools as tools_module
from ..common.checks import readiness_error
from ..common.platforms import RECOGNIZED_TARGETS, effective_target, normalize_target
from ..common.procs import run_capture, run_streaming
from ..common.redaction import redact_argv
from ..common.results import Cancelled, Result, ScaffoldError, repair
from . import buildopts, freshness, macos, patches
from .cmd_tools import local_shims
from .records import Operation, OutputState, output_states

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


def readiness_gate(ctx, target):
    """Required macOS host and build-readiness checks; blockers stop before anything is prepared."""
    from . import doctor
    checks = []
    groups = ("host-mac", "mac-build") if target == "mac" else ("android-build",)
    for group in groups:
        checks.extend(doctor.GROUP_FUNCTIONS[group](ctx, "mac" if target == "mac" else "android"))
    error = readiness_error(checks)
    if error is not None:
        error.details["checks"] = [check.to_dict() for check in checks if check.status != "pass"]
        raise error
    return checks


@dataclass
class Prepared:
    loaded: dict
    toolchain: object
    checks: list = field(default_factory=list)


def prepare_environment(ctx, identity, target):
    loaded = env_module.load_environment(identity, ctx.environ, ctx.log)
    toolchain, tool_checks = tools_module.require_toolchain(identity, ctx.log)
    checks = readiness_gate(ctx, target)
    return Prepared(loaded, toolchain, [*tool_checks, *checks])


def package_environment(prepared, shims):
    return tools_module.child_environment(prepared.loaded, prepared.toolchain, shims)


def run_package_step(ctx, identity, prepared, arguments, extra_env=None):
    """Run one package command in Core and return its exit code."""
    argv = tools_module.package_argv(prepared.toolchain, arguments)
    with local_shims(prepared.toolchain) as shims:
        env = package_environment(prepared, shims)
        for name, value in (extra_env or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return argv, run_streaming(argv, str(identity.core), env, ctx.log, json_mode=ctx.json_mode)


def metal_environment(ctx):
    """Point the build at an installed Metal toolchain when `xcrun metal` cannot find one."""
    probe = run_capture(["xcrun", "metal", "--version"], os.getcwd(), ctx.environ, ctx.log, timeout=60)
    if probe.returncode == 0:
        return {}
    for info in sorted(Path("/private/var/run/com.apple.security.cryptexd/mnt").glob(
            "com.apple.MobileAsset.MetalToolchain-*/Metal.xctoolchain/ToolchainInfo.plist")):
        result = run_capture(["/usr/libexec/PlistBuddy", "-c", "Print :Identifier", str(info)], os.getcwd(),
                             ctx.environ, ctx.log, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            existing = ctx.environ.get("TOOLCHAINS")
            identifier = result.stdout.strip()
            return {"TOOLCHAINS": "%s:%s" % (existing, identifier) if existing else identifier}
    return {}


# --- patch preparation -----------------------------------------------------------------


def prepare_patches(ctx, identity, prepared, op):
    """Apply Core patches only when needed and only when no local work is at risk."""
    plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root)
    op.step("patch-preparation-plan", action=plan.action, reason=plan.reason)
    if plan.action == "current":
        state = patches.snapshot_files(identity, plan.report)
        patches.write_receipt(identity, plan.trees, state, ctx.state_root)
        return False, plan
    if plan.action == "conflict":
        raise patches.conflict_error(plan, identity)
    op.step("apply-patches", reason=plan.reason)
    argv, code = run_package_step(ctx, identity, prepared, ["run", "apply_patches"])
    if code != 0:
        raise ScaffoldError("CHILD_FAILED", "Applying Core patches failed (exit %d)." % code,
                            details={"argv": argv, "phase": "patches"}, child_exit_code=code)
    after = patches.collect_drift(identity)
    if after.files:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Patches were applied but %d file(s) still differ from their metadata." % len(after.files),
                            details={"files": sorted(after.files)[:50]},
                            repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)])])
    patches.write_receipt(identity, plan.trees, patches.snapshot_files(identity, after), ctx.state_root, {})
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


def artifact_for(effective):
    return verify_mac_artifact(effective) if effective.target == "mac" else _android().verify_artifact(effective)


def _android():
    from . import android
    return android


# --- build phase ---------------------------------------------------------------------


def build_arguments(effective, subcommand, script_args, force_gn):
    arguments = ["run", subcommand, *script_args, *effective.generated]
    if force_gn:
        arguments.append("--force_gn_gen")
    return [*arguments, *effective.forwarded]


@dataclass
class BuildOutcome:
    argv: list
    child_exit: int
    artifact: dict | None
    unresolved_reason: str | None
    effective: object
    prepared_patches: bool


def run_output_step(ctx, identity, effective, prepared, op, arguments, phase, extra_env=None, before_child=None):
    """Run the package command that writes the output directory.

    The attempt is recorded before the child starts, so an earlier success stops
    being proof of the output's contents even if this process dies mid-write.
    """
    state = OutputState(identity, effective.output_dir, ctx.state_root) if effective.output_dir else None
    if state is not None:
        state.begin_attempt(op.id)
    op.step(phase, package_arguments=arguments)
    try:
        if before_child is not None:
            before_child()
        argv, code = run_package_step(ctx, identity, prepared, arguments, extra_env)
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


def perform_build(ctx, identity, effective, prepared, op, force_gn=False):
    """Prepare sources, run the package build, then verify the resulting output."""
    changed, plan = prepare_patches(ctx, identity, prepared, op)
    android = effective.target == "android"
    if android:
        refreshed = _android().prepare_support(ctx, identity, prepared, op, effective)
        if refreshed:
            patches.record_extra_expected(identity, ctx.state_root)
        changed = refreshed or changed
    arguments = build_arguments(effective, "build", (), force_gn or changed)
    op.update(effective={"target": effective.target, "configuration": effective.configuration,
                         "arch": effective.arch, "output_dir": str(effective.output_dir),
                         "package_arguments": arguments})
    argv, state = run_output_step(
        ctx, identity, effective, prepared, op, arguments, "build",
        _android().build_environment(ctx) if android else metal_environment(ctx),
        (lambda: _android().write_gn_overrides(identity, effective)) if android else None)
    try:
        artifact, reason = artifact_for(effective)
    except ScaffoldError as error:
        if state is not None:
            state.end_attempt(op.id, "output-invalid")
        error.child_exit_code = 0
        error.details.setdefault("argv", argv)
        raise
    inputs = freshness.compute(identity, plan.report.patched_paths, arguments, ctx.log)
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


def finish_build_result(command, outcome, identity, op, prepared):
    result = Result(command=command, operation_id=op.id, child_exit_code=outcome.child_exit,
                    checks=[check.to_dict() for check in prepared.checks])
    result.data = {"build": build_data(outcome, identity)}
    if outcome.artifact:
        result.artifacts = [outcome.artifact]
        result.text = "Build completed. Verified output: %s" % outcome.artifact["path"]
    else:
        result.add_warning("ARTIFACT_UNRESOLVED", result.data["build"]["explanation"])
        result.text = result.data["build"]["explanation"]
    return result


# --- command handlers ------------------------------------------------------------------


def plan_result(command, ctx, identity, effective, steps):
    result = Result(command=command)
    result.data = {"plan": {"steps": steps, "effective": {
        "target": effective.target, "configuration": effective.configuration, "arch": effective.arch,
        "output_dir": str(effective.output_dir) if effective.output_dir else None, "sources": effective.sources}}}
    lines = ["Plan for %s (nothing was run):" % command]
    for index, step in enumerate(steps, 1):
        lines.append("  %d. %s%s" % (index, step["name"], " - " + step["detail"] if step.get("detail") else ""))
    result.text = "\n".join(lines)
    return result


def build_plan_steps(ctx, identity, effective, subcommand="build", script_args=(), sync_args=None):
    steps = []
    if sync_args is not None:
        steps.append({"name": "sync", "writes": ["source tree"], "detail": "bpm " + " ".join(redact_argv(sync_args))})
    unresolved = []
    try:
        env_module.require_environment(identity, ctx.environ, ctx.log)
        steps.append({"name": "environment", "status": "approved"})
    except ScaffoldError as error:
        unresolved.append(error.message)
        steps.append({"name": "environment", "status": "unresolved", "detail": error.message})
    try:
        plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root)
        steps.append({"name": "patch-preparation", "status": plan.action, "detail": plan.reason,
                      "writes": ["Chromium patched files"] if plan.action == "apply" else []})
    except ScaffoldError as error:
        steps.append({"name": "patch-preparation", "status": "unresolved", "detail": error.message})
    arguments = build_arguments(effective, subcommand, script_args, False)
    steps.append({"name": subcommand, "argv_arguments": arguments, "cwd": str(identity.core),
                  "writes": [str(effective.output_dir)]})
    steps.append({"name": "verify-output", "detail": "expects an application in %s" % effective.output_dir})
    return steps


def do_build(ctx, command, sync_first=False, run_after=False):
    parsed = ctx.parsed
    target_token = parsed.positionals[0] if parsed.positionals else None
    identity, effective = select_build(ctx, target_token, parsed.forwarded)
    if parsed.get("plan"):
        sync_plan = sync_arguments(ctx, effective.target, [], identity) if sync_first else None
        return plan_result(command, ctx, identity, effective,
                           build_plan_steps(ctx, identity, effective, "build", (), sync_plan))
    if parsed.get("device") and effective.target != "android":
        raise ScaffoldError("INVALID_INPUT", "--device applies to Android only.")
    device = _android().preflight_device(ctx) if run_after and effective.target == "android" else None
    prepared = prepare_environment(ctx, identity, effective.target)
    op = Operation(command, identity, {"target": effective.target, "configuration": effective.configuration},
                   ctx.state_root)
    sync_result = None
    try:
        if sync_first:
            sync_result = do_sync_phase(ctx, identity, prepared, op, effective.target, [])
        outcome = perform_build(ctx, identity, effective, prepared, op, bool(parsed.get("force_gn")))
        if run_after and outcome.artifact is None:
            raise unresolved_error(outcome, identity)
    except Cancelled:
        op.finish("cancelled", 130)
        raise
    except ScaffoldError as error:
        op.finish("error", error.exit_code)
        error.details.setdefault("operation_id", op.id)
        raise
    result = finish_build_result(command, outcome, identity, op, prepared)
    if sync_result:
        result.data["sync"] = sync_result
    if run_after:
        result = run_phase(ctx, identity, outcome.artifact, result, device)
    op.finish(result.status, result.exit_code, ctx.log.records, [outcome.artifact] if outcome.artifact else [])
    return result


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


def cmd_test(ctx):
    parsed = ctx.parsed
    target, _ = effective_target(parsed.get("target"), ctx.config)
    if target != "mac":
        raise ScaffoldError("UNSUPPORTED_CAPABILITY",
                            "Android tests are not available in this release; nothing was prepared or built.",
                            details={"target": target},
                            repairs=[repair(["bdev", "test", "mac", parsed.get("suite")],
                                            note="Run the suite on macOS instead.")])
    identity, effective = select_build(ctx, parsed.get("target"), parsed.forwarded)
    script_args = [parsed.get("suite")]
    if parsed.get("filter"):
        script_args.append("--filter=%s" % parsed.get("filter"))
    if parsed.get("plan"):
        return plan_result("test", ctx, identity, effective,
                           build_plan_steps(ctx, identity, effective, "test", script_args))
    prepared = prepare_environment(ctx, identity, effective.target)
    op = Operation("test", identity, {"target": effective.target, "suite": parsed.get("suite")}, ctx.state_root)
    try:
        arguments = build_arguments(effective, "test", script_args, False)
        outcome = run_test_package(ctx, identity, effective, prepared, op, arguments)
    except Cancelled:
        op.finish("cancelled", 130)
        raise
    except ScaffoldError as error:
        op.finish("error", error.exit_code)
        raise
    result = Result(command="test", operation_id=op.id, child_exit_code=0,
                    checks=[check.to_dict() for check in prepared.checks])
    result.data = {"suite": parsed.get("suite"), "argv": outcome, "cwd": str(identity.core)}
    result.text = "Test suite %s passed." % parsed.get("suite")
    op.finish("ok", 0, ctx.log.records)
    return result


def run_test_package(ctx, identity, effective, prepared, op, arguments):
    prepare_patches(ctx, identity, prepared, op)
    argv, state = run_output_step(ctx, identity, effective, prepared, op, arguments, "test",
                                  metal_environment(ctx) if effective.target == "mac" else {})
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


def artifact_freshness(ctx, identity, output_dir):
    state = OutputState(identity, output_dir, ctx.state_root)
    report = patches.collect_drift(identity)
    recorded = (state.success or {}).get("fingerprint")
    current = freshness.compute(identity, report.patched_paths, [], ctx.log)
    return freshness.assess(recorded, current, state)


def add_freshness_warning(result, assessment):
    if assessment["status"] == "stale":
        result.add_warning("STALE_BUILD", "This output does not include the latest changes: %s" %
                           "; ".join(assessment["evidence"]), freshness=assessment)
    elif assessment["status"] == "unknown":
        result.add_warning("UNKNOWN_FRESHNESS", freshness.UNKNOWN_MESSAGE, freshness=assessment)


def run_phase(ctx, identity, bundle, result, device=None):
    """Restart the browser (or reinstall the APK) with a validated output and add the outcome to a result."""
    if bundle.get("kind") == "apk":
        return _android().restart_apk(ctx, identity, bundle, result, device)
    env_module.require_environment(identity, ctx.environ, ctx.log)
    assessment = artifact_freshness(ctx, identity, Path(bundle["path"]).parent)
    add_freshness_warning(result, assessment)
    outcome = macos.restart(bundle, ctx.environ, ctx.log)
    result.data = {**(result.data or {}), "run": {"artifact": bundle, "freshness": assessment, **outcome}}
    if not result.artifacts:
        result.artifacts = [{**bundle, "verified": False, "freshness": assessment["status"]}]
    result.text = ((result.text + "\n") if result.text else "") + "Restarted %s (%s)." % (
        bundle["name"], bundle["path"])
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
    if target == "android":
        return _android().run_android(ctx, identity)
    configuration = requested_configuration(ctx) or "Debug"
    bundle = select_artifact(ctx, identity, target, configuration, "arm64")
    if parsed.get("plan"):
        result = Result(command="run", data={"plan": {"artifact": bundle, "steps": ["stop matching instances",
                                                                                     "launch selected bundle"]}})
        result.text = "Plan for run (nothing was run): restart %s" % bundle["path"]
        return result
    result = Result(command="run")
    return run_phase(ctx, identity, bundle, result)


# --- sync ------------------------------------------------------------------------------


def gclient_targets(identity):
    """Existing target_os values from the checkout's .gclient (literals only; nothing is executed)."""
    import ast
    path = identity.workspace / ".gclient"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "target_os" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
            return [value] if isinstance(value, str) else list(value)
    return []


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


def local_work_conflicts(ctx, identity):
    """Evidence of local work that a source sync could overwrite."""
    status = run_capture(["git", "-C", str(identity.core), "status", "--porcelain"], str(identity.core), None,
                         ctx.log, timeout=120)
    conflicts = []
    if status.returncode == 0 and status.stdout.strip():
        conflicts.append({"path": str(identity.core), "reason": "Core has %d uncommitted change(s)" %
                          len(status.stdout.splitlines())})
    plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root)
    if plan.action == "conflict":
        conflicts.extend(plan.conflicts)
    return conflicts


def do_sync_phase(ctx, identity, prepared, op, target, forwarded):
    conflicts = local_work_conflicts(ctx, identity)
    if conflicts:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Sync could overwrite local work in %d place(s); nothing was changed." % len(conflicts),
                            details={"files": conflicts[:50]},
                            repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)])])
    before = {"core_head": freshness.resolve_head(identity.core), "chromium_head": freshness.resolve_head(identity.src)}
    arguments = sync_arguments(ctx, target, forwarded, identity)
    op.step("sync", arguments=arguments, before=before)
    argv, code = run_package_step(ctx, identity, prepared, arguments)
    if code != 0:
        raise ScaffoldError("CHILD_FAILED", "The sync command exited with status %d." % code,
                            details={"argv": argv, "phase": "sync"}, child_exit_code=code)
    after = {"core_head": freshness.resolve_head(identity.core), "chromium_head": freshness.resolve_head(identity.src)}
    op.step("sync-complete", after=after)
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
        result = Result(command="sync")
        result.data = {"plan": {"argv_arguments": sync_arguments(ctx, mobile, parsed.forwarded, identity),
                                "cwd": str(identity.core), "writes": ["source tree and dependencies"]}}
        result.text = "Plan for sync (nothing was run): bpm " + " ".join(redact_argv(result.data["plan"]["argv_arguments"]))
        return result
    prepared = prepare_environment(ctx, identity, "mac")
    op = Operation("sync", identity, {"targets": targets}, ctx.state_root)
    try:
        phase = do_sync_phase(ctx, identity, prepared, op, mobile, parsed.forwarded)
    except Cancelled:
        op.finish("cancelled", 130)
        raise
    except ScaffoldError as error:
        op.finish("error", error.exit_code)
        raise
    result = Result(command="sync", operation_id=op.id, child_exit_code=0, data={"sync": phase, "targets": targets},
                    checks=[check.to_dict() for check in prepared.checks])
    result.text = "Sync completed for %s." % ", ".join(targets)
    op.finish("ok", 0, ctx.log.records)
    return result


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
    for entry in files:
        lines.append("  %s (%s) %s" % (entry["path"], ", ".join(entry["reasons"]), entry["numstat"]))
        if ctx.parsed.get("diff"):
            diff = run_capture(["git", "-C", str(identity.src), "diff", "--", entry["path"]], str(identity.src), None,
                               ctx.log, timeout=120)
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
    prepared = prepare_environment(ctx, identity, "mac")
    op = Operation("patches update", identity, {}, ctx.state_root)
    argv, code = run_package_step(ctx, identity, prepared, ["run", "update_patches", *ctx.parsed.forwarded])
    if code != 0:
        op.finish("error", 5)
        raise ScaffoldError("CHILD_FAILED", "update_patches exited with status %d." % code,
                            details={"argv": argv}, child_exit_code=code)
    status = run_capture(["git", "-C", str(identity.core), "status", "--short", "--branch"], str(identity.core), None,
                         ctx.log, timeout=120)
    changes = [line for line in status.stdout.splitlines()[1:]]
    result = Result(command="patches update", operation_id=op.id, child_exit_code=0,
                    data={"argv": argv, "changed_files": changes})
    result.text = ("Patch changes for review (nothing was committed):\n  " + "\n  ".join(changes)) if changes \
        else "No patch changes detected."
    op.finish("ok", 0, ctx.log.records)
    return result
