# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android build preparation, APK outputs, and install/restart."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from ..common.platforms import host_architecture, host_platform
from ..common.procs import run_capture
from ..common.results import Result, ScaffoldError, repair
from . import adb, android_deps
from .records import OutputState, output_states, track

PACKAGE_PREFIX = "com.brave."
DEFAULT_JAVA_OPTS = "-Xmx10G -Xms1G"
DEFAULT_SISO_LOCAL_JOBS = "8"
# Variables Chromium's Android lint sets itself; a second preference home makes it fail.
CLEARED_VARIABLES = ("ANDROID_PREFS_ROOT", "ANDROID_SDK_HOME", "ANDROID_USER_HOME")


def require_available():
    if host_platform() != "mac" or host_architecture() != "arm64":
        raise ScaffoldError("UNSUPPORTED_CAPABILITY",
                            "Android builds are supported from macOS arm64 hosts only.",
                            details={"host": "%s/%s" % (host_platform(), host_architecture())})


def apk_path(output_dir, arch):
    return Path(output_dir) / "apks" / ("BraveMono%s.apk" % arch)


def aapt2_path(identity):
    for candidate in (identity.src / "third_party" / "android_build_tools" / "aapt2" / "cipd" / "aapt2",
                      *sorted((identity.src / "third_party" / "android_sdk" / "public" / "build-tools").glob("*/aapt2"))):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def read_apk(path, identity=None, environ=None, log=None):
    """Validate an APK and return its identity. Package names need the checkout's aapt2."""
    path = Path(path)
    if not path.is_file():
        raise ScaffoldError("ARTIFACT_MISSING", "The APK %s does not exist." % path, details={"path": str(path)})
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError) as error:
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s is not a readable APK (%s)." % (path, error),
                            details={"path": str(path)})
    if "AndroidManifest.xml" not in names:
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s has no AndroidManifest.xml." % path, details={"path": str(path)})
    package = None
    aapt2 = aapt2_path(identity) if identity is not None else None
    if aapt2 is not None:
        probe = run_capture([str(aapt2), "dump", "packagename", str(path)], os.getcwd(), environ, log, timeout=60)
        package = probe.stdout.strip() if probe.returncode == 0 and probe.stdout.strip() else None
    if package is not None and not package.startswith(PACKAGE_PREFIX):
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s is package %s, not a Brave application." % (path, package),
                            details={"path": str(path), "package": package})
    return {"path": str(path), "kind": "apk", "name": path.name, "package": package,
            "package_verified": package is not None}


UNPROVEN_PACKAGE = "the APK's package name could not be verified (the checkout's aapt2 is missing or failed)"


def verify_artifact(effective, identity, environ, log=None):
    if effective.unresolved:
        return None, "; ".join(effective.unresolved)
    artifact = read_apk(apk_path(effective.output_dir, effective.arch), identity, environ, log)
    if not artifact["package_verified"]:
        return None, UNPROVEN_PACKAGE
    artifact.update(target="android", configuration=effective.configuration, arch=effective.arch,
                    output_dir=str(effective.output_dir), verified=True)
    return artifact, None


def build_environment(ctx):
    """Child-only adjustments for Android builds; None removes a variable."""
    extra = {name: None for name in CLEARED_VARIABLES}
    extra["JAVA_OPTS"] = ctx.environ.get("JAVA_OPTS") or DEFAULT_JAVA_OPTS
    limits = ctx.environ.get("SISO_LIMITS", "")
    if "local=" not in limits:
        jobs = ctx.environ.get("SCAFFOLD_ANDROID_SISO_LOCAL_JOBS", DEFAULT_SISO_LOCAL_JOBS)
        if not jobs.isdigit() or int(jobs) < 1:
            raise ScaffoldError("INVALID_INPUT", "SCAFFOLD_ANDROID_SISO_LOCAL_JOBS must be a positive integer.")
        extra["SISO_LIMITS"] = ("local=%s,%s" % (jobs, limits)) if limits else "local=%s" % jobs
    return extra


def prepare_support(ctx, identity, prepared, op, effective):
    """Refresh support patches/resources when needed; returns True when the checkout was changed."""
    plan = android_deps.plan_preparation(ctx, identity, ctx.log)
    op.step("android-support-plan", action=plan.action, reason=plan.reason,
            working_copy=plan.evidence.get("working_copy"))
    if plan.action == "conflict":
        raise android_deps.conflict_error(plan, identity)
    refreshed = False
    if plan.action == "refresh":
        op.step("android-support-refresh", reason=plan.reason, scripts=list(plan.scripts),
                writes=["src third_party resources and patched files listed by the support scripts"])
        android_deps.refresh(ctx, identity, prepared.loaded, plan, ctx.log)
        refreshed = True
    return refreshed


def write_gn_overrides(identity, effective):
    return android_deps.ensure_args_gn(identity, effective.output_dir, effective.chosen_gn_keys)


# --- devices and run --------------------------------------------------------------------------


def preflight_device(ctx):
    """Select the device before anything is stopped or built."""
    adapter = adb.require_adb(ctx.environ)
    device, source = adb.select_device(adapter, ctx.environ, ctx.parsed.get("device"),
                                       ctx.config.default_android_device, ctx.log)
    return adapter, device, source


def apk_candidates(ctx, identity, configuration, arch):
    found = {}
    from . import buildopts
    default = identity.src / "out" / buildopts.default_build_dir("android", configuration, arch)
    paths = [apk_path(default, arch)]
    for state in output_states(identity, ctx.state_root):
        artifact = (state.get("success") or {}).get("artifact") or {}
        if artifact.get("target") == "android" and artifact.get("configuration") == configuration \
                and artifact.get("arch") == arch:
            paths.append(Path(artifact["path"]))
    for path in paths:
        try:
            found[os.path.realpath(path)] = read_apk(path, identity, ctx.environ, ctx.log)
        except ScaffoldError:
            continue
    return list(found.values())


def select_apk(ctx, identity, configuration, arch):
    explicit = ctx.parsed.get("artifact")
    if explicit:
        path = Path(os.path.expanduser(explicit))
        return read_apk(path if path.is_absolute() else Path(ctx.cwd) / path, identity, ctx.environ, ctx.log)
    candidates = apk_candidates(ctx, identity, configuration, arch)
    if not candidates:
        raise ScaffoldError(
            "ARTIFACT_MISSING", "No android %s %s APK was found for this checkout." % (configuration, arch),
            repairs=[repair(["bdev", "build", "android", "--checkout", str(identity.core)]),
                     repair(["bdev", "build-run", "android", "--checkout", str(identity.core)])])
    if len(candidates) > 1:
        raise ScaffoldError("ARTIFACT_AMBIGUOUS", "More than one APK matches; choose one with --artifact.",
                            details={"candidates": [item["path"] for item in candidates]},
                            repairs=[repair(["bdev", "run", "android", "--artifact", candidates[0]["path"]],
                                            note="Example only; choose the APK you intend to install.")])
    return candidates[0]


def restart_apk(ctx, identity, artifact, result, device=None):
    """Install the APK on the selected device and restart its package there."""
    from . import cmd_build
    if not artifact["package_verified"]:
        raise ScaffoldError(
            "ARTIFACT_UNRESOLVED", "Nothing was installed or restarted: %s." % UNPROVEN_PACKAGE,
            details={"artifact": artifact["path"]},
            repairs=[repair(["bdev", "tools", "setup", "--checkout", str(identity.core)], requires_user_action=False,
                            note="Explicit repair of checkout-local tools; run it only if aapt2 is missing.")])
    adapter, device, source = device or preflight_device(ctx)
    output_dir = artifact.get("output_dir") or str(Path(artifact["path"]).parent.parent)
    assessment = cmd_build.artifact_freshness(ctx, identity, output_dir, android=True)
    cmd_build.add_freshness_warning(result, assessment)
    outcome = adb.restart_package(adapter, device["id"], artifact["path"], artifact["package"], ctx.environ, ctx.log)
    result.data = {**(result.data or {}), "run": {"artifact": artifact, "freshness": assessment,
                                                  "device_selection": source, **outcome}}
    if not result.artifacts:
        result.artifacts = [{**artifact, "verified": False, "freshness": assessment["status"]}]
    result.text = ((result.text + "\n") if result.text else "") + "Installed and restarted %s on %s." % (
        artifact["package"], device["id"])
    return result


def run_android(ctx, identity):
    configuration = (ctx.parsed.get("configuration") or "debug").capitalize()
    artifact = select_apk(ctx, identity, configuration, "arm64")
    if ctx.parsed.get("plan"):
        result = Result(command=ctx.command, data={"plan": {"artifact": artifact, "steps": [
            "select one device", "install over the existing app (data is kept)", "stop this package on that device",
            "launch and verify the process"]}})
        result.text = "Plan for %s (nothing was run): install %s" % (ctx.command, artifact["path"])
        return result
    with track(ctx, ctx.command, identity, {"target": "android", "artifact": artifact["path"]}) as op:
        op.step("run", artifact=artifact["path"])
        return op.complete(restart_apk(ctx, identity, artifact, Result(command=ctx.command)))


# --- explicit dependency setup ----------------------------------------------------------------


def cmd_android_setup(ctx):
    identity = ctx.identity()
    with track(ctx, "android setup", identity, {"ref": ctx.parsed.get("ref"), "source": ctx.parsed.get("source")}) as op:
        outcome = android_deps.setup_working_copy(identity, ctx.state_root, ctx.parsed.get("source"),
                                                  ctx.parsed.get("ref"), ctx.log)
        wc = android_deps.working_copy(identity)
        ok, detail = android_deps.run_gate(wc, "copyMacRes.sh", ctx.environ, ctx.log)
        outcome["compatible"] = ok
        outcome["compatibility_detail"] = detail
        op.detail(support_head=outcome["working_copy"]["head"], compatible=ok)
        result = Result(command="android setup", data=outcome)
        facts = outcome["working_copy"]
        result.text = "Support working copy: %s (%s @ %s)\nShared object cache: %s (%s)\nCompatibility gate: %s" % (
            wc, facts["branch"] or "detached", (facts["head"] or "")[:12], outcome["cache"], outcome["cache_action"],
            "passed" if ok else "FAILED - " + str(detail))
        if not ok:
            result.add_warning("DEPENDENCY_INCOMPATIBLE",
                               "This support revision does not match the checkout: %s" % detail)
        return op.complete(result)
