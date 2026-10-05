# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""iOS Simulator support: project paths, bootstrap state, and simulator selection."""

from __future__ import annotations

import json
import os
import plistlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair

RUNTIME_PREFIX = "com.apple.CoreSimulator.SimRuntime.iOS-"
XCFRAMEWORKS = ("BraveCore", "NalaAssets", "PartitionAllocSupport")
DEPLOYMENT_TARGET = re.compile(r"\bIPHONEOS_DEPLOYMENT_TARGET\s*=\s*([0-9]+(?:\.[0-9]+)*)\s*;")
PHYSICAL_DEVICE_WORDS = ("iphoneos", "physical", "device")


def app_directory(identity):
    return identity.core / "ios" / "brave-ios" / "App"


def project_path(identity):
    return app_directory(identity) / "Client.xcodeproj"


def current_link(identity):
    """Core's stable symlink to the output directory of the latest Xcode build."""
    return identity.src / "out" / "ios_current_link"


def bootstrap_artifacts(identity):
    """Files Core's `ios_bootstrap` creates so the Swift package manifest resolves."""
    link = current_link(identity)
    return [link / "args.xcconfig", *(link / ("%s.xcframework" % name) / "Info.plist" for name in XCFRAMEWORKS),
            app_directory(identity) / "Configuration" / "LLDBInit"]


def missing_bootstrap_artifacts(identity):
    return [path for path in bootstrap_artifacts(identity) if not path.exists()]


def bootstrap_repair(identity):
    return repair(["bdev", "bpm", "run", "ios_bootstrap", "--checkout", str(identity.core)],
                  note="Runs Core's iOS bootstrap, which writes placeholders under out/ios_current_link and "
                       "Configuration/LLDBInit in Core.")


def output_directory_name(configuration, arch="arm64"):
    """The GN output Core's Xcode pre-action builds for the simulator (named by Core's own rule)."""
    return "ios_%s_%s_simulator" % (configuration, arch)


def deployment_target(project_file):
    """The highest IPHONEOS_DEPLOYMENT_TARGET in the project file, or None."""
    try:
        versions = DEPLOYMENT_TARGET.findall(Path(project_file).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return max(versions, key=version_key) if versions else None


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def _available(entry):
    if "isAvailable" in entry:
        return bool(entry["isAvailable"])
    if entry.get("availabilityError"):
        return False
    return entry.get("availability") in (None, "(available)", "available")


def _runtime_version(runtime):
    version = runtime.get("version")
    if isinstance(version, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
        return version
    match = re.search(r"([0-9]+(?:\.[0-9]+)*)", runtime.get("name", ""))
    return match.group(1) if match else None


def parse_simulators(data):
    """(runtimes, devices) of available iOS simulators from `simctl list --json runtimes devices`."""
    runtimes, devices = {}, []
    for runtime in data.get("runtimes", []):
        identifier = runtime.get("identifier", "")
        version = _runtime_version(runtime)
        if identifier.startswith(RUNTIME_PREFIX) and version and _available(runtime):
            runtimes[identifier] = {"identifier": identifier, "version": version,
                                    "name": runtime.get("name", identifier)}
    for identifier, entries in data.get("devices", {}).items():
        if identifier not in runtimes:
            continue
        for entry in entries:
            if _available(entry) and isinstance(entry.get("udid"), str) and isinstance(entry.get("name"), str):
                devices.append({"udid": entry["udid"], "name": entry["name"], "state": entry.get("state", "unknown"),
                                "runtime": identifier, "version": runtimes[identifier]["version"]})
    return runtimes, devices


def compatible(devices, minimum):
    if not minimum:
        return list(devices)
    return [device for device in devices if version_key(device["version"]) >= version_key(minimum)]


def list_simulators(environ, log=None):
    """Available iOS simulators, or a ScaffoldError when `simctl` cannot answer."""
    result = run_capture(["xcrun", "simctl", "list", "--json", "runtimes", "devices"], ".", environ, log,
                         timeout=60, max_bytes=16 << 20)
    if result.returncode != 0 or result.timed_out or result.truncated:
        raise ScaffoldError("DEVICE_UNAVAILABLE", "Could not query iOS Simulators with 'xcrun simctl list' "
                            "(this is not evidence that none are installed).",
                            details={"exit": result.returncode, "stderr": result.stderr.strip()[-300:]})
    try:
        return parse_simulators(json.loads(result.stdout))
    except (ValueError, AttributeError) as error:
        raise ScaffoldError("DEVICE_UNAVAILABLE", "'xcrun simctl list' returned output that could not be read (%s)."
                            % error) from error


def select_simulator(devices, requested=None, minimum=None):
    """One simulator: the requested name or UDID, else a booted iPhone, else the newest available iPhone."""
    if requested and any(word in requested.lower() for word in PHYSICAL_DEVICE_WORDS):
        raise ScaffoldError("INVALID_INPUT", "Physical iOS devices are not supported; name an iOS Simulator "
                            "or give its UDID.", details={"requested": requested})
    usable = compatible(devices, minimum)
    if requested:
        matches = [device for device in usable if requested in (device["udid"], device["name"])]
        if not matches:
            raise ScaffoldError(
                "DEVICE_UNAVAILABLE", "No available iOS Simulator matches %r%s." % (
                    requested, " for deployment target iOS %s or later" % minimum if minimum else ""),
                details={"available": [{"udid": d["udid"], "name": d["name"], "version": d["version"]}
                                       for d in usable]})
        if len(matches) > 1:
            raise ScaffoldError("DEVICE_AMBIGUOUS", "%r names %d simulators; use a UDID." % (requested, len(matches)),
                                details={"udids": [device["udid"] for device in matches]})
        return matches[0]
    if not usable:
        raise ScaffoldError(
            "DEVICE_UNAVAILABLE", "No available iOS Simulator runtime%s. Install one in Xcode (Settings > Components)."
            % (" supports iOS %s or later" % minimum if minimum else ""))

    def order(device):
        phone = device["name"].startswith("iPhone")
        booted = device["state"] == "Booted"
        return (0 if booted and phone else 1 if phone else 2, tuple(-part for part in version_key(device["version"])),
                device["name"], device["udid"])
    return sorted(usable, key=order)[0]


# --- build selection ------------------------------------------------------------------

CONFIGURATION = "Debug"
ARCH = "arm64"
SCHEME = "Debug"
# Options that change what xcodebuild builds, so the output would no longer be the Debug simulator app.
CONFLICTING_OPTIONS = ("-project", "-workspace", "-scheme", "-target", "-alltargets", "-configuration", "-sdk",
                       "-arch")
# Options that take a separate value, so the value is not mistaken for an action.
VALUE_OPTIONS = frozenset(CONFLICTING_OPTIONS) | {
    "-destination", "-derivedDataPath", "-jobs", "-xcconfig", "-toolchain", "-resultBundlePath",
    "-clonedSourcePackagesDirPath", "-archivePath", "-exportPath", "-exportOptionsPlist", "-testPlan",
    "-only-testing", "-skip-testing", "-destination-timeout", "-maximum-concurrent-test-device-destinations",
    "-packageCachePath", "-resultStreamPath", "-defaultPackageRegistryURL", "-localizationPath", "-exportLanguage",
    "-downloadPlatform", "-buildVersion"}
BUILD_ACTIONS = ("build", "clean")
OTHER_ACTIONS = ("analyze", "archive", "install", "installsrc", "installhdrs", "installapi", "docbuild", "test",
                 "build-for-testing", "test-without-building")
# Modes that print information and write nothing.
INFORMATION_MODES = ("-showBuildSettings", "-showBuildSettingsForIndex", "-list", "-showsdks", "-showdestinations",
                     "-showTestPlans", "-version", "-help", "-usage", "-checkFirstLaunchStatus", "-dry-run", "-n")
# Modes that may write but do not build the app.
WRITING_MODES = ("-resolvePackageDependencies", "-exportArchive", "-exportLocalizations", "-importLocalizations",
                 "-downloadPlatform", "-downloadAllPlatforms", "-runFirstLaunch")
# These settings cannot relocate or select the app. Other assignments may change the products, including
# through project-defined settings, so a fixed default path is not evidence of what that invocation built.
OUTPUT_NEUTRAL_SETTINGS = frozenset(("CODE_SIGNING_ALLOWED", "CODE_SIGNING_REQUIRED", "CODE_SIGN_IDENTITY"))


@dataclass
class IosBuild:
    project: Path
    gn_output: Path
    derived_data: Path
    app_path: Path
    forwarded: list
    derived_forwarded: bool = False
    destination_forwarded: bool = False
    actions: list = field(default_factory=list)
    changes_output: bool = True
    unresolved: list = field(default_factory=list)
    requested_device: str | None = None

    @property
    def builds(self):
        return not self.unresolved

    @property
    def outputs(self):
        return [self.gn_output, self.derived_data]

    def describe(self):
        return {"target": "ios", "configuration": CONFIGURATION, "arch": ARCH, "output_dir": str(self.derived_data),
                "sources": {"derived_data": "forwarded" if self.derived_forwarded else "default",
                            "destination": "forwarded" if self.destination_forwarded else
                            "device" if self.requested_device else "default"},
                "gn_output": str(self.gn_output), "derived_data": str(self.derived_data),
                "app": str(self.app_path)}


def default_derived_data(identity):
    return identity.src / "out" / ("ios_%s_xcode_derived_data" % CONFIGURATION)


def app_in(derived_data):
    return Path(derived_data) / "Build" / "Products" / ("%s-iphonesimulator" % CONFIGURATION) / "Client.app"


def resolve_build(parsed, identity, run_after=False):
    """Validate the request and choose outputs; every conflict fails here, before anything is changed."""
    if parsed.get("configuration") not in (None, "debug"):
        raise ScaffoldError("UNSUPPORTED_CAPABILITY", "iOS supports Debug simulator builds only.",
                            details={"configuration": parsed.get("configuration")})
    for option, name in (("offline", "--offline"), ("force_gn", "--force-gn"),
                         ("skip_support_refresh", "--skip-support-refresh")):
        if parsed.get(option):
            raise ScaffoldError("INVALID_INPUT", "%s does not apply to iOS: Xcode starts Core's build with its own "
                                "environment. Unknown options go to xcodebuild." % name)
    tokens = list(parsed.forwarded)
    conflicts, actions, information, writing, output_overrides = [], [], [], [], []
    derived = None
    destination = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            if token in CONFLICTING_OPTIONS:
                conflicts.append(token)
            elif token == "-destination":
                destination = True
            elif token == "-derivedDataPath":
                if index + 1 >= len(tokens):
                    raise ScaffoldError("INVALID_INPUT", "-derivedDataPath needs a directory.")
                derived = tokens[index + 1]
            elif token in INFORMATION_MODES:
                information.append(token)
            elif token in WRITING_MODES:
                writing.append(token)
            elif token == "-xcconfig":
                output_overrides.append(token)
            if token in VALUE_OPTIONS:
                index += 1
        elif token in BUILD_ACTIONS or token in OTHER_ACTIONS:
            actions.append(token)
        elif "=" in token and token.split("=", 1)[0] not in OUTPUT_NEUTRAL_SETTINGS:
            output_overrides.append(token.split("=", 1)[0])
        index += 1
    if conflicts:
        raise ScaffoldError(
            "SELECTOR_CONFLICT", "%s would change what is built. bdev builds the Debug scheme for the iOS "
            "Simulator; run xcodebuild yourself for anything else." % ", ".join(conflicts),
            details={"options": conflicts})
    device = parsed.get("device")
    if destination and device:
        raise ScaffoldError("SELECTOR_CONFLICT", "--device and a forwarded -destination both choose the "
                            "simulator; use one.")
    if destination and run_after:
        raise ScaffoldError("SELECTOR_CONFLICT", "A forwarded -destination leaves the simulator to run on "
                            "unidentified; choose it with --device instead.")
    derived_path = Path(derived).expanduser() if derived else default_derived_data(identity)
    if not derived_path.is_absolute():
        derived_path = identity.core / derived_path
    derived_path = Path(os.path.normpath(derived_path))
    build = IosBuild(project=project_path(identity), gn_output=identity.src / "out" / output_directory_name(CONFIGURATION),
                     derived_data=derived_path, app_path=app_in(derived_path), forwarded=tokens,
                     derived_forwarded=derived is not None, destination_forwarded=destination, actions=actions,
                     requested_device=device)
    if information or writing:
        build.changes_output = bool(writing)
        build.unresolved.append("%s does not build the app" % ", ".join(information + writing))
    elif actions and "build" not in actions:
        build.unresolved.append("the action %s does not build the app" % " and ".join(actions))
    if output_overrides:
        build.unresolved.append("the app output cannot be identified with %s" % ", ".join(output_overrides))
    return build


def xcodebuild_argv(build, simulator=None):
    """The complete xcodebuild command; `simulator` is the chosen device (a placeholder while planning)."""
    argv = ["xcodebuild", "-project", str(build.project), "-scheme", SCHEME, "-configuration", CONFIGURATION,
            "-sdk", "iphonesimulator"]
    if not build.destination_forwarded:
        argv += ["-destination", "platform=iOS Simulator,id=%s" % (simulator["udid"] if simulator else "<simulator>")]
    if not build.derived_forwarded:
        argv += ["-derivedDataPath", str(build.derived_data)]
    argv += build.forwarded
    if not build.actions and not any(token in INFORMATION_MODES or token in WRITING_MODES
                                     for token in build.forwarded):
        argv.append("build")
    return argv


# --- artifacts ------------------------------------------------------------------------


def read_bundle(path):
    """Validate a Brave iOS Simulator app bundle and return its identity."""
    path = Path(path)
    if not path.is_dir():
        raise ScaffoldError("ARTIFACT_MISSING", "The application %s does not exist." % path,
                            details={"path": str(path)})
    try:
        info = plistlib.loads((path / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s has no readable Info.plist (%s)." % (path, error),
                            details={"path": str(path)})
    identifier, executable = info.get("CFBundleIdentifier"), info.get("CFBundleExecutable")
    if not identifier or not executable:
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s is missing its bundle identifier or executable name." % path,
                            details={"path": str(path)})
    if not identifier.startswith("com.brave."):
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s is not a Brave application (%s)." % (path, identifier),
                            details={"path": str(path), "bundle_identifier": identifier})
    platforms = info.get("CFBundleSupportedPlatforms")
    if platforms is not None and "iPhoneSimulator" not in platforms:
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s was not built for the iOS Simulator (%s)." % (
            path, ", ".join(platforms)), details={"path": str(path), "platforms": platforms})
    if not os.access(path / executable, os.X_OK):
        raise ScaffoldError("ARTIFACT_MISMATCH", "The executable %s is missing or not executable." % (path / executable),
                            details={"path": str(path)})
    return {"path": str(path), "kind": "app", "bundle_identifier": identifier, "executable": str(path / executable),
            "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or path.stem,
            "version": info.get("CFBundleShortVersionString")}


def verify_artifact(identity, build):
    """(artifact, None) for a verified build; (None, reason) when the request builds no app."""
    if build.unresolved:
        return None, "; ".join(build.unresolved)
    bundle = read_bundle(build.app_path)
    link = current_link(identity)
    if os.path.realpath(link) != os.path.realpath(build.gn_output):
        raise ScaffoldError(
            "ARTIFACT_MISMATCH", "Core's ios_current_link points at %s, not at the expected GN output %s, so the "
            "built app does not match this build." % (os.path.realpath(link) if link.exists() else "nothing",
                                                      build.gn_output),
            details={"link": str(link), "expected": str(build.gn_output)})
    bundle.update(target="ios", configuration=CONFIGURATION, arch=ARCH, output_dir=str(build.derived_data),
                  gn_output_dir=str(build.gn_output), verified=True)
    return bundle, None


# --- running on a simulator -------------------------------------------------------------

BOOT_WAIT_SECONDS = 300
ALREADY_BOOTED = 149


def _simctl(environ, log, args, timeout=120):
    return run_capture(["xcrun", "simctl", *args], os.getcwd(), environ, log, timeout=timeout)


def restart_app(udid, bundle, environ, log=None, progress=None, started=None, failed=None):
    """Boot the simulator, install over the existing app (data is kept), and launch it, replacing a running copy."""
    progress = progress or (lambda name, **outcome: None)
    started = started or (lambda name: None)
    failed = failed or (lambda name, **outcome: None)

    def stop(name, result, message):
        failed(name, exit=result.returncode)
        raise ScaffoldError("LAUNCH_FAILED", "%s: %s" % (message, (result.stderr or result.stdout).strip()[-400:]
                                                         or "exit %d" % result.returncode),
                            details={"simulator": udid, "phase": name, "exit": result.returncode},
                            child_exit_code=result.returncode)

    started("boot-simulator")
    booted = _simctl(environ, log, ["boot", udid])
    if booted.returncode not in (0, ALREADY_BOOTED) and "current state: Booted" not in booted.stderr:
        stop("boot-simulator", booted, "Booting simulator %s failed" % udid)
    ready = _simctl(environ, log, ["bootstatus", udid, "-b"], timeout=BOOT_WAIT_SECONDS)
    if ready.returncode != 0 or ready.timed_out:
        stop("boot-simulator", ready, "Simulator %s did not finish booting" % udid)
    progress("boot-simulator", exit=booted.returncode)
    started("install-app")
    installed = _simctl(environ, log, ["install", udid, bundle["path"]], timeout=600)
    if installed.returncode != 0:
        stop("install-app", installed, "Installing %s on simulator %s failed" % (bundle["path"], udid))
    progress("install-app", exit=0)
    started("launch-app")
    launched = _simctl(environ, log, ["launch", "--terminate-running-process", udid, bundle["bundle_identifier"]])
    if launched.returncode != 0:
        stop("launch-app", launched, "Launching %s on simulator %s failed" % (bundle["bundle_identifier"], udid))
    found = re.search(r":\s*(\d+)\s*$", launched.stdout.strip())
    if not found:
        failed("launch-app", exit=0)
        raise ScaffoldError("LAUNCH_FAILED", "%s was launched on simulator %s but no process id was reported." % (
            bundle["bundle_identifier"], udid), details={"simulator": udid, "phase": "launch-app",
                                                         "stdout": launched.stdout.strip()[-300:]})
    progress("launch-app", exit=0, pid=found.group(1))
    return {"simulator_udid": udid, "bundle_identifier": bundle["bundle_identifier"],
            "launched_pid": int(found.group(1))}
