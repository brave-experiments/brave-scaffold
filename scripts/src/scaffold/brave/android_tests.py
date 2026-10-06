# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android tests on macOS: the required support branch, the Core test overlay, suites, device arguments, results."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path

from ..common.procs import run_capture, run_streaming
from ..common.results import ScaffoldError, repair
from ..common.platforms import host_platform
from . import android_deps, steps as step_module, support_scripts, sync_scope
from .patchformat import UnknownPatchFormat, parse_patch_targets

TEST_SUPPORT_BRANCH = android_deps.metadata()["default_ref"]
OVERLAY_SCRIPT = "applyBraveCoreTestSupport.sh"
OVERLAY_PATCH = "patches/brave-core-android-tests-on-mac.patch"
OVERLAY_STEP = "android-test-overlay"
RESULTS_NAME = "scaffold_test_results.json"
HOST, DEVICE = "host", "device"
SUITES = {"brave_junit_tests": HOST, "brave_java_unit_tests": DEVICE}
RUNNERS = {"brave_junit_tests": "bin/run_brave_junit_tests", "brave_java_unit_tests": "bin/run_brave_java_unit_tests"}
DEVICE_TOKENS = ("--device", "-s", "--adb-path", "--manual_android_test_device")
PASSED = ("SUCCESS",)
SKIPPED = ("SKIP", "NOTRUN")


def suite_kind(suite):
    """`host` runs on this Mac; `device` runs on the selected emulator or device."""
    if suite not in SUITES:
        raise ScaffoldError(
            "UNSUPPORTED_CAPABILITY", "The Android test suite %r is not available; nothing was prepared or built." % suite,
            details={"suites": {name: ("no device" if kind == HOST else "needs a device") for name, kind in SUITES.items()}},
            repairs=[repair(["bdev", "test", "android", "brave_junit_tests"], note="Host-side Robolectric/JUnit tests."),
                     repair(["bdev", "test", "android", "brave_java_unit_tests", "--device", "<serial>"],
                            note="Instrumented tests on a device; placeholder serial.")])
    return SUITES[suite]


def check_options(parsed, kind):
    if parsed.get("all_devices") and parsed.get("device"):
        raise ScaffoldError("SELECTOR_CONFLICT", "Use either --device or --all-devices, not both.")
    if kind == HOST and parsed.get("all_devices"):
        raise ScaffoldError("INVALID_INPUT", "--all-devices applies to device-backed Android tests only.")
    if kind == HOST and parsed.get("device"):
        raise ScaffoldError("INVALID_INPUT", "--device does not apply to %s: it runs on this Mac and uses no device."
                            % parsed.get("suite"), details={"example": "bdev test android %s" % parsed.get("suite")})
    for token in parsed.forwarded:
        name = token.partition("=")[0]
        if name in DEVICE_TOKENS:
            raise ScaffoldError(
                "SELECTOR_CONFLICT", "%s is chosen by the scaffold and cannot be forwarded to the test command." % token,
                details={"example": "bdev test android brave_java_unit_tests --device <serial>"})


def device_arguments(adb, device):
    """The runner's device options, placed after every option the test command itself parses."""
    return ["--device", device["id"], "--adb-path", str(adb)]


def forwarded_results_file(forwarded):
    return any(token.partition("=")[0] == "--json-results-file" for token in forwarded)


def device_results_path(effective, device, base=None):
    suffix = hashlib.sha256(device.encode()).hexdigest()[:16]
    path = Path(base) if base else effective.preparation_dir / RESULTS_NAME
    if not path.is_absolute():
        path = effective.preparation_dir / path
    return path.with_name("%s_%s%s" % (path.stem, suffix, path.suffix))


def results_option(forwarded):
    path = None
    for index, token in enumerate(forwarded):
        if token == "--json-results-file" and index + 1 < len(forwarded):
            path = forwarded[index + 1]
        elif token.startswith("--json-results-file="):
            path = token.partition("=")[2]
    return path


# --- support branch -----------------------------------------------------------------------------


def require_support_branch(identity, log=None):
    """The selected checkout's support working copy must be on the test branch. Nothing is switched."""
    wc = android_deps.working_copy(identity)
    facts = android_deps.inspect_working_copy(wc, log)
    if facts is None:
        raise android_deps.missing_working_copy(identity, wc)
    if facts["branch"] != TEST_SUPPORT_BRANCH:
        current = "branch %s" % facts["branch"] if facts["branch"] else "a detached HEAD (%s)" % (facts["head"] or "unknown")[:12]
        raise ScaffoldError(
            "DEPENDENCY_INCOMPATIBLE",
            "Android tests need the support working copy %s on the %s branch, but it is on %s. "
            "Nothing was switched, prepared, or built." % (wc, TEST_SUPPORT_BRANCH, current),
            details={"working_copy": str(wc), "required_branch": TEST_SUPPORT_BRANCH, "branch": facts["branch"],
                     "head": facts["head"], "checkout": str(identity.core)},
            repairs=[repair(["git", "-C", str(wc), "switch", TEST_SUPPORT_BRANCH], requires_user_action=True,
                            note="The scaffold never switches this repository. Run it yourself when the working "
                                 "copy has no changes you need to keep.")])
    return facts


# --- Core test overlay --------------------------------------------------------------------------


def require_overlay_host():
    if host_platform() != "mac":
        raise ScaffoldError("UNSUPPORTED_CAPABILITY", "The Android test overlay is available on macOS hosts only.")


def overlay_scope(identity, wc):
    """Source-relative files the overlay may write: the reviewed list, which its patch must stay within."""
    declared = support_scripts.require_contract(wc, OVERLAY_SCRIPT)["direct"]
    patch = Path(wc) / OVERLAY_PATCH
    try:
        targets = {"brave/" + name for name in parse_patch_targets(patch.read_text(encoding="utf-8"))}
    except (OSError, UnknownPatchFormat) as error:
        raise ScaffoldError("PREPARATION_CONFLICT", "The Core test overlay patch cannot be read: %s." % error,
                            details={"files": [{"path": OVERLAY_PATCH, "reason": str(error)}]})
    extra = sorted(targets - set(declared))
    if extra:
        raise ScaffoldError(
            "PREPARATION_CONFLICT",
            "The Core test overlay patch writes %d file(s) outside its reviewed list, so it was not applied." % len(extra),
            details={"files": [{"path": name, "reason": "not in the reviewed overlay write list"} for name in extra]})
    return list(declared)


def overlay_state(identity, wc, environ, log=None):
    """`applied`, `absent`, or `conflict`, from the overlay script's read-only check."""
    result = run_capture(["bash", "./" + OVERLAY_SCRIPT, "--src-root", str(identity.src), "--check"], str(wc), environ,
                         log, timeout=120)
    return {0: "applied", 1: "absent"}.get(result.returncode, "conflict")


def overlay_step(identity, wc, state, writes):
    status = {"applied": "current", "absent": "planned", "conflict": "blocked"}[state]
    detail = {"applied": "The overlay is already applied in Core.",
              "absent": "Core does not have the Android test overlay yet.",
              "conflict": "Core has a partial or conflicting overlay; nothing will be changed."}[state]
    return step_module.Step(
        OVERLAY_STEP, "Apply the support repository's Android test overlay to Core for this test run.", status,
        reads=[str(Path(wc) / OVERLAY_PATCH)], writes=[str(identity.src / name) for name in writes] if state == "absent" else [],
        argv=["bash", "./" + OVERLAY_SCRIPT, "--src-root", str(identity.src), "--apply"] if state == "absent" else None,
        cwd=str(wc) if state == "absent" else None, needs=["android-support"],
        on_failure="Stops before the build; a conflicting overlay is never forced.",
        cleanup="Reverse the overlay after the run only if this command applied it; preserve a pre-existing overlay.",
        detail=detail)


def overlay_plan_step(ctx, identity):
    """The overlay step for a plan, or a blocked step naming why it cannot be judged."""
    wc = android_deps.working_copy(identity)
    try:
        writes = overlay_scope(identity, wc)
        return overlay_step(identity, wc, overlay_state(identity, wc, ctx.environ, ctx.log), writes)
    except ScaffoldError as error:
        return step_module.Step(OVERLAY_STEP, "Apply the support repository's Android test overlay to Core.", "blocked",
                                needs=["android-support"], detail=error.message)


def remove_overlay(ctx, execution, op):
    """Reverse an overlay owned by this run, refusing conflicting local edits."""
    require_overlay_host()
    identity = execution.identity
    wc = android_deps.working_copy(identity)
    name = "android-test-overlay-cleanup"
    op.start(name)
    ctx.log.phase("Starting to remove the temporary Android test overlay...")
    for path in overlay_scope(identity, wc):
        ctx.log.phase("  " + path.removeprefix("brave/"))
    if overlay_state(identity, wc, execution.environ, ctx.log) != "applied":
        op.fail(name, code="PREPARATION_CONFLICT")
        ctx.log.phase("Overlay cleanup stopped: conflicting edits remain in Core; review the files above.")
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "The Android test overlay changed during the run; cleanup left it in place. "
                            "Review the Core files before reversing it.")
    argv = ["bash", "./" + OVERLAY_SCRIPT, "--src-root", str(identity.src), "--reverse"]
    code = run_streaming(argv, str(wc), execution.environ, ctx.log, json_mode=ctx.json_mode)
    if code != 0 or overlay_state(identity, wc, execution.environ, ctx.log) != "absent":
        op.fail(name, exit=code)
        ctx.log.phase("Overlay cleanup failed: inspect the Core files above before retrying.")
        raise ScaffoldError("PREPARATION_CONFLICT", "Android test overlay cleanup failed; inspect the Core files.",
                            details={"argv": argv, "cwd": str(wc)}, child_exit_code=code)
    op.succeed(name, exit=0)
    ctx.log.phase("Done - temporary Android test overlay removed; added files removed and patched files restored.")


def prepare_overlay(ctx, execution, op):
    """Apply the Core test overlay when absent. Returns True when Core was changed."""
    require_overlay_host()
    identity = execution.identity
    ctx = execution.context(ctx)
    wc = android_deps.working_copy(identity)
    writes = overlay_scope(identity, wc)
    state = overlay_state(identity, wc, ctx.environ, ctx.log)
    described = overlay_step(identity, wc, state, writes)
    op.note("android-test-overlay-plan", state=state, **described.record())
    if state == "conflict":
        raise ScaffoldError(
            "PREPARATION_CONFLICT", "Core has a partial or conflicting Android test overlay. Nothing was changed.",
            details={"files": [{"path": name, "reason": "overlay conflicts with local changes"} for name in writes],
                     "checkout": str(identity.core)},
            repairs=[repair(["git", "-C", str(identity.core), "status", "--short"],
                            note="Review the local changes; the scaffold does not force the overlay over them.")])
    if state == "applied":
        ctx.log.phase("Android test overlay is already applied; it will stay in place after this run.")
        return False
    op.start(OVERLAY_STEP, **described.record())
    ctx.log.phase("Starting to apply the Android test overlay from local support branch %s "
                  "(remote branch: origin/%s)..." % (TEST_SUPPORT_BRANCH, TEST_SUPPORT_BRANCH))
    ctx.log.phase("Core files:")
    for path in writes:
        ctx.log.phase("  " + path.removeprefix("brave/"))
    scope = sync_scope.sync_repositories(identity)
    before = sync_scope.snapshot(identity, scope, ctx.log, include_core=True)
    argv = ["bash", "./" + OVERLAY_SCRIPT, "--src-root", str(identity.src), "--apply"]
    code = run_streaming(argv, str(wc), ctx.environ, ctx.log, json_mode=ctx.json_mode)
    if code != 0:
        op.fail(OVERLAY_STEP, exit=code)
        raise ScaffoldError("CHILD_FAILED", "%s failed (exit %d)." % (OVERLAY_SCRIPT, code),
                            details={"argv": argv, "cwd": str(wc)}, child_exit_code=code)
    changed = sync_scope.changed_between(identity, before,
                                         sync_scope.snapshot(identity, scope, ctx.log, include_core=True))
    allowed = {identity.src / name for name in writes}
    strays = sorted(str(path.relative_to(identity.src)) for path in changed if path not in allowed)
    if strays:
        op.fail(OVERLAY_STEP, code="PREPARATION_CONFLICT")
        raise ScaffoldError(
            "PREPARATION_CONFLICT", "The Core test overlay changed %d tracked file(s) outside its reviewed list." % len(strays),
            details={"files": [{"path": name, "reason": "changed by the overlay but not declared"} for name in strays[:50]],
                     "checkout": str(identity.core)},
            repairs=[repair(["git", "-C", str(identity.src), "status", "--short"], note="Nothing was reverted.")])
    op.succeed(OVERLAY_STEP, exit=0)
    ctx.log.phase("Done - Android test overlay applied. These changes will be cleaned up when this run finishes.")
    return True


# --- results ------------------------------------------------------------------------------------


def results_path(effective):
    return Path(effective.preparation_dir) / RESULTS_NAME


def summarize_results(path):
    """Counts from a Chromium JSON results file, or None when it is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        iterations = data["per_iteration_data"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    try:
        for iteration in iterations:
            for runs in iteration.values():
                status = runs[-1]["status"] if runs else "UNKNOWN"
                counts["passed" if status in PASSED else "skipped" if status in SKIPPED else "failed"] += 1
    except (AttributeError, KeyError, IndexError, TypeError):
        return None
    return {"path": str(path), **counts, "ran": counts["passed"] + counts["failed"]}


def runner_path(effective, suite):
    return Path(effective.preparation_dir) / RUNNERS[suite]


def verify_outcome(effective, suite, path, verifiable):
    """After a zero exit, check what the runner itself can't be trusted to say. Returns (summary, warning)."""
    runner = runner_path(effective, suite)
    if not runner.is_file():
        raise ScaffoldError("ARTIFACT_MISSING", "The test command exited 0 but %s was not produced, so no tests ran." % runner,
                            details={"runner": str(runner)}, child_exit_code=0)
    if not verifiable:
        return None, None
    summary = summarize_results(path)
    if summary is None:
        return None, "The runner exited 0 but wrote no readable results file (%s), so the test count is unverified." % path
    if summary["failed"]:
        raise ScaffoldError("TEST_FAILED", "%d test(s) failed although the runner exited 0." % summary["failed"],
                            details={"results": summary}, child_exit_code=0)
    if summary["ran"] == 0:
        raise ScaffoldError(
            "NO_TESTS_RAN", "The runner exited 0 but ran no tests; the filter may match nothing. "
            "Host-side filters need a fully qualified class or a wildcard such as '*ExampleTest*'.",
            details={"results": summary}, child_exit_code=0)
    return summary, None
