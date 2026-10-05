# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""iOS Simulator support: project paths, bootstrap state, and simulator selection."""

from __future__ import annotations

import json
import re
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
