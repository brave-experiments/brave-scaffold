# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android build preparation, APK outputs, and install/restart."""

from __future__ import annotations

import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..common.platforms import host_architecture, host_platform
from ..common.config import save_android_device
from ..common.procs import run_capture
from ..common.results import Result, ScaffoldError, repair
from . import adb, android_deps, steps as step_module
from .records import output_states, track

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


def prepare_support(ctx, execution, op, effective):
    """Refresh support patches/resources when needed; returns True when the checkout was changed."""
    identity = execution.identity
    ctx = execution.context(ctx)
    plan = android_deps.plan_preparation(ctx, identity, ctx.log)
    wc = android_deps.working_copy(identity)
    writes = [str(identity.src / key) for key in android_deps.planned_writes(identity, wc, plan.scripts, ctx.log)] \
        if plan.action == "refresh" else []
    described = step_module.support_step(identity, plan, writes).record()
    op.note("android-support-plan", action=plan.action, reason=plan.reason, working_copy=plan.evidence.get("working_copy"),
            **described)
    if plan.action == "conflict":
        raise android_deps.conflict_error(plan, identity)
    refreshed = False
    if plan.action == "refresh":
        op.start("android-support-refresh", scripts=list(plan.scripts), **described)
        refresh = android_deps.refresh(ctx, identity, execution.environ, plan, ctx.log)
        op.succeed("android-support-refresh", recorded=refresh["recorded"])
        refreshed = True
    return refreshed


def write_gn_overrides(identity, effective):
    return android_deps.ensure_args_gn(identity, effective.preparation_dir, effective.chosen_gn_keys)


# --- devices and run --------------------------------------------------------------------------


def preflight_device(ctx, allow_all=False):
    """Select the device before anything is stopped or built."""
    adapter = adb.require_adb(ctx.environ)
    devices = adb.list_devices(adapter, ctx.environ, ctx.log)
    try:
        device, source = adb.choose_device(devices, ctx.parsed.get("device"), ctx.config.default_android_device)
    except ScaffoldError as error:
        interactive = not ctx.json_mode and not ctx.parsed.get("plan") and sys.stdin.isatty() and sys.stderr.isatty()
        if error.code != "DEVICE_AMBIGUOUS" or not interactive:
            raise
        device = adb.pick_device(adapter, devices, ctx.environ, ctx.log, allow_all=allow_all)
        if device is None:
            return DeviceGroup(adapter, devices, [])
        source = "interactive"
        print("Remember this device in %s? [y/N]: " % ctx.config.path,
              end="", file=sys.stderr, flush=True)
        if sys.stdin.readline().strip().lower() in ("y", "yes"):
            try:
                save_android_device(ctx.config.path, device["id"])
            except (OSError, ScaffoldError) as save_error:
                print("Could not save the default: %s. Using this device for this command." % save_error,
                      file=sys.stderr)
            else:
                ctx.config.default_android_device = device["id"]
                print("Saved default device: %s." % device["id"], file=sys.stderr)
    return adapter, device, source


@dataclass
class DeviceGroup:
    adapter: str
    devices: list
    skipped: list


BUILD_ABIS = {"arm64": "arm64-v8a", "arm": "armeabi-v7a", "x64": "x86_64", "x86": "x86"}


def preflight_deployment(ctx, arch=None):
    if not ctx.parsed.get("all_devices"):
        choice = preflight_device(ctx, allow_all=True)
        if not isinstance(choice, DeviceGroup):
            return choice
        adapter, available = choice.adapter, choice.devices
    else:
        if ctx.parsed.get("device"):
            raise ScaffoldError("SELECTOR_CONFLICT", "Use either --device or --all-devices, not both.")
        adapter = adb.require_adb(ctx.environ)
        available = adb.list_devices(adapter, ctx.environ, ctx.log)
    devices, skipped = [], []
    abi = BUILD_ABIS.get(arch) if arch else None
    if arch and abi is None:
        raise ScaffoldError("ARTIFACT_UNRESOLVED", "Cannot check device compatibility for architecture %s." % arch)
    for device in available:
        reason = None
        if device["state"] != "device":
            reason = device["state"]
        else:
            try:
                device = adb.device_capabilities(adapter, device, ctx.environ, ctx.log)
            except ScaffoldError as error:
                reason = error.message
            if reason is None and abi and abi not in device["abis"]:
                reason = "requires %s; device supports %s" % (abi, ", ".join(device["abis"]))
        if reason:
            skipped.append({"device": device["id"], "status": "skipped", "reason": reason})
        else:
            devices.append(device)
    if not devices:
        raise ScaffoldError("DEVICE_UNAVAILABLE", "No compatible, usable Android devices are connected.",
                            details={"devices": skipped})
    return DeviceGroup(adapter, devices, skipped)


def apk_requirements(ctx, identity, artifact):
    """Read compatibility from the actual APK, including independently built outputs."""
    tool = aapt2_path(identity)
    probe = run_capture([str(tool), "dump", "badging", artifact["path"]], ctx.cwd, ctx.environ, ctx.log, timeout=60) \
        if tool else None
    sdk = re.search(r"^sdkVersion:'(\d+)'", probe.stdout, re.MULTILINE) \
        if probe and probe.returncode == 0 and not probe.truncated else None
    native = re.search(r"^native-code:[ \t]*(.*)$", probe.stdout, re.MULTILINE) if sdk else None
    if sdk is None:
        raise ScaffoldError("ARTIFACT_UNRESOLVED", "Cannot read the APK's minimum Android version; nothing was installed.")
    try:
        with zipfile.ZipFile(artifact["path"]) as archive:
            abis = sorted({name.split('/')[1] for name in archive.namelist()
                           if name.startswith('lib/') and len(name.split('/')) > 2 and name.endswith('.so')})
    except (OSError, zipfile.BadZipFile) as error:
        raise ScaffoldError("ARTIFACT_UNRESOLVED", "Cannot read the APK's ABIs: %s." % error)
    if native:
        abis = re.findall(r"'([^']+)'", native[1])
        if not abis:
            raise ScaffoldError("ARTIFACT_UNRESOLVED", "Cannot read the APK's ABIs; nothing was installed.")
    return abis, int(sdk[1])


def deployment_steps(choice, artifact, package):
    if isinstance(choice, DeviceGroup):
        devices, source = choice.devices, "all-devices"
    else:
        _, device, source = choice
        devices = [device]
    steps = []
    if isinstance(choice, DeviceGroup):
        steps.append(step_module.Step("verify-device-compatibility", "Check the APK's ABIs and minimum Android version before installing.",
                                      "planned", reads=[artifact], detail="Incompatible devices are skipped."))
    for device in devices:
        install = step_module.install_apk_step(device["id"], artifact)
        if isinstance(choice, DeviceGroup):
            install.needs.append("verify-device-compatibility")
        steps += [step_module.select_device_step({"id": device["id"], "source": source}), install,
                  step_module.stop_package_step(device["id"], package),
                  step_module.launch_package_step(device["id"], package)]
    if isinstance(choice, DeviceGroup):
        for item in choice.skipped:
            steps.append(step_module.Step("skip-device", "Skip %s: %s." % (item["device"], item["reason"]), "resolved"))
    return steps


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


def restart_apk(ctx, identity, artifact, result, device=None, op=None):
    """Install the APK on the selected device and restart its package there."""
    if not artifact["package_verified"]:
        raise ScaffoldError(
            "ARTIFACT_UNRESOLVED", "Nothing was installed or restarted: %s." % UNPROVEN_PACKAGE,
            details={"artifact": artifact["path"]},
            repairs=[repair(["bdev", "build", "android", "--checkout", str(identity.core)], requires_user_action=False,
                            note="aapt2 comes from the Android support resources, which the build prepares before "
                                 "compiling; this builds too. It is not part of 'bdev tools setup'.")])
    choice = device or preflight_deployment(ctx)
    if isinstance(choice, DeviceGroup):
        return restart_all_devices(ctx, identity, artifact, result, choice, op)
    adapter, chosen, source = choice
    outcome = restart_device(ctx, artifact, adapter, chosen, op)
    if op is not None:
        op.succeed("run", artifact=artifact["path"])
    result.data = {**(result.data or {}), "run": {"artifact": artifact,
                                               "device_selection": source, **outcome}}
    if not result.artifacts:
        result.artifacts = [{**artifact, "verified": False}]
    result.text = ((result.text + "\n") if result.text else "") + "Installed and restarted %s on %s." % (
        artifact["package"], chosen["id"])
    return result


def restart_device(ctx, artifact, adapter, device, op=None):
    """Record install, stop, and launch phases for one device."""
    descriptions = {step.name: step for step in (
        step_module.install_apk_step(device["id"], artifact["path"]),
        step_module.stop_package_step(device["id"], artifact["package"]),
        step_module.launch_package_step(device["id"], artifact["package"]))}

    def start_phase(name):
        if op is not None:
            op.start(name, device=device["id"], **descriptions[name].record())

    return adb.restart_package(adapter, device["id"], artifact["path"], artifact["package"], ctx.environ, ctx.log,
                               progress=op.succeed if op is not None else None, started=start_phase,
                               failed=op.fail if op is not None else None)


def restart_all_devices(ctx, identity, artifact, result, group, op=None):
    abis, min_sdk = apk_requirements(ctx, identity, artifact)
    outcomes = list(group.skipped)
    for device in group.devices:
        reason = None
        if abis and not set(abis).intersection(device["abis"]):
            reason = "APK requires %s; device supports %s" % (", ".join(abis), ", ".join(device["abis"]))
        elif device["sdk"] < min_sdk:
            reason = "APK requires Android API %d; device has API %d" % (min_sdk, device["sdk"])
        if reason:
            outcomes.append({"device": device["id"], "status": "skipped", "reason": reason})
            if op is not None:
                op.detail(device_results=outcomes)
            continue
        try:
            outcome = restart_device(ctx, artifact, group.adapter, device, op)
        except ScaffoldError as error:
            outcomes.append({"device": device["id"], "status": "error", "code": error.code,
                             "reason": error.message, "child_exit_code": error.child_exit_code,
                             "details": error.details})
        else:
            outcomes.append({**outcome, "status": "ok"})
        if op is not None:
            op.detail(device_results=outcomes)
    failures = [item for item in outcomes if item["status"] == "error"]
    succeeded = [item for item in outcomes if item["status"] == "ok"]
    result.data = {**(result.data or {}), "run": {"artifact": artifact, "device_selection": "all-devices",
                                               "devices": outcomes}}
    if not result.artifacts:
        result.artifacts = [{**artifact, "verified": False}]
    summary = ["%s: %s%s" % (item["device"], "installed and restarted" if item["status"] == "ok" else item["status"],
                              " - " + item["reason"] if item.get("reason") else "") for item in outcomes]
    result.text = ((result.text + "\n") if result.text else "") + "\n".join(summary)
    if failures or not succeeded:
        error = ScaffoldError("LAUNCH_FAILED" if failures else "DEVICE_UNAVAILABLE",
                              "Deployment failed on %d device(s)." % len(failures) if failures else
                              "No connected device is compatible with this APK.", details={"devices": outcomes},
                              child_exit_code=failures[0]["child_exit_code"] if failures else None)
        result.status, result.exit_code = "error", error.exit_code
        result.child_exit_code = error.child_exit_code
        result.error = {"code": error.code, "message": error.message, "details": error.details, "repairs": []}
    if op is not None:
        op.detail(device_results=outcomes)
        (op.fail if result.error else op.succeed)("run", artifact=artifact["path"], devices=outcomes)
    return result


def run_plan(ctx, identity, artifact):
    """What installing and restarting the selected APK would do, changing nothing."""
    package = artifact["package"] or "<package from the APK>"
    steps = [step_module.plan_environment_step(ctx, identity),
             step_module.Step("select-artifact", "Use the selected APK; run never builds.", "resolved",
                              reads=[artifact["path"]], needs=["environment"], detail=artifact["path"])]
    try:
        choice = preflight_deployment(ctx)
        steps += deployment_steps(choice, artifact["path"], package)
    except ScaffoldError as error:
        steps.append(step_module.select_device_step(error=error))
    result = Result(command=ctx.command, data={"plan": {"artifact": artifact, "steps": [s.to_dict() for s in steps]}})
    result.text = step_module.render_plan(ctx.command, steps)
    return result


def run_android(ctx, identity, validated=False):
    configuration = (ctx.parsed.get("configuration") or "debug").capitalize()
    artifact = select_apk(ctx, identity, configuration, "arm64")
    if ctx.parsed.get("plan"):
        return run_plan(ctx, identity, artifact)
    with track(ctx, ctx.command, identity, {"target": "android", "artifact": artifact["path"]},
               validated=validated) as op:
        op.start("run", artifact=artifact["path"])
        return op.complete(restart_apk(ctx, identity, artifact, Result(command=ctx.command), None, op))


# --- explicit dependency setup ----------------------------------------------------------------


def cmd_android_setup(ctx):
    if host_platform() != "mac":
        raise ScaffoldError("UNSUPPORTED_CAPABILITY", "Android-on-Mac support setup is only needed on macOS.")
    identity = ctx.identity()
    with track(ctx, "android setup", identity, {"ref": ctx.parsed.get("ref"), "source": ctx.parsed.get("source")}) as op:
        outcome = android_deps.setup_working_copy(identity, ctx.config, ctx.parsed.get("source"),
                                                  ctx.parsed.get("ref"), ctx.log)
        wc = android_deps.working_copy(identity)
        ok, detail = android_deps.run_gate(wc, "copyMacRes.sh", ctx.environ, ctx.log)
        outcome["compatible"] = ok
        outcome["compatibility_detail"] = detail
        op.detail(support_head=outcome["working_copy"]["head"], compatible=ok)
        result = Result(command="android setup", data=outcome)
        facts = outcome["working_copy"]
        result.text = "Shared support checkout: %s (%s @ %s)\nWorkspace link: %s\nCompatibility gate: %s" % (
            outcome["shared_checkout"], facts["branch"] or "detached", (facts["head"] or "")[:12], wc,
            "passed" if ok else "FAILED - " + str(detail))
        if not ok:
            result.add_warning("DEPENDENCY_INCOMPATIBLE",
                               "This support revision does not match the checkout: %s" % detail)
        return op.complete(result)
