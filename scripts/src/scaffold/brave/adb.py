# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android device discovery, selection, and app restart through adb."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair

VERIFY_SECONDS = 15
RECOVERY = {
    "offline": "Reconnect the cable or restart the device, then run 'adb reconnect offline'.",
    "unauthorized": "Unlock the device and accept the USB debugging prompt, then try again.",
}


def find_adb(environ):
    """adb from ADB, ANDROID_HOME, ANDROID_SDK_ROOT, or PATH, in that order."""
    candidates = [environ.get("ADB")]
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if environ.get(name):
            candidates.append(str(Path(environ[name]) / "platform-tools" / "adb"))
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("adb", path=environ.get("PATH"))


def require_adb(environ):
    adb = find_adb(environ)
    if adb is None:
        raise ScaffoldError(
            "LOCAL_TOOL_MISSING", "adb was not found. Install the Android platform tools or set ANDROID_HOME.",
            details={"searched": ["ADB", "ANDROID_HOME", "ANDROID_SDK_ROOT", "PATH"]})
    return adb


def list_devices(adb, environ, log=None):
    result = run_capture([adb, "devices"], os.getcwd(), environ, log, timeout=30)
    if result.returncode != 0:
        raise ScaffoldError("DEVICE_UNAVAILABLE", "'adb devices' failed: %s" % result.stderr.strip(),
                            details={"exit": result.returncode})
    devices = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices.append({"id": parts[0], "state": parts[1]})
    return devices


def _listing(devices):
    return ["%s (%s)" % (item["id"], item["state"]) for item in devices]


def choose_device(devices, requested=None, configured=None):
    """Pick a device only when the choice is unambiguous."""
    wanted = requested or configured
    source = "flag" if requested else "configuration"
    if wanted:
        found = next((item for item in devices if item["id"] == wanted), None)
        if found is None:
            raise ScaffoldError("DEVICE_UNAVAILABLE", "The device %s (from %s) is not connected." % (wanted, source),
                                details={"devices": _listing(devices)},
                                repairs=[repair(["adb", "devices"])])
        return _require_usable(found), source
    usable = [item for item in devices if item["state"] == "device"]
    if len(usable) == 1:
        return usable[0], "only-device"
    if len(usable) > 1:
        raise ScaffoldError(
            "DEVICE_AMBIGUOUS", "More than one device is available; choose one with --device.",
            details={"devices": _listing(devices), "example": "--device %s" % usable[0]["id"]},
            repairs=[repair(["adb", "devices"], note="Lists devices; then pass --device <id>.")])
    raise ScaffoldError(
        "DEVICE_UNAVAILABLE", "No usable Android device is connected.",
        details={"devices": _listing(devices), "recovery": sorted({RECOVERY[i["state"]] for i in devices
                                                                  if i["state"] in RECOVERY})},
        repairs=[repair(["adb", "devices"])])


def _require_usable(device):
    if device["state"] != "device":
        raise ScaffoldError(
            "DEVICE_UNAVAILABLE", "The device %s is %s." % (device["id"], device["state"]),
            details={"device": device, "recovery": RECOVERY.get(device["state"], "Check the device connection.")},
            repairs=[repair(["adb", "devices"])])
    return device


def select_device(adb, environ, requested, configured, log=None):
    return choose_device(list_devices(adb, environ, log), requested, configured)


def _adb(adb, device, args, environ, log, timeout=120):
    return run_capture([adb, "-s", device, *args], os.getcwd(), environ, log, timeout=timeout)


def restart_package(adb, device, apk, package, environ, log=None, verify_seconds=VERIFY_SECONDS):
    """Install over the existing app (data is kept), stop only this package, launch, verify."""
    installed = _adb(adb, device, ["install", "-d", "-r", "-g", apk], environ, log, timeout=600)
    if installed.returncode != 0 or "Success" not in installed.stdout:
        output = (installed.stdout + installed.stderr).strip()
        raise ScaffoldError("LAUNCH_FAILED", "Installing %s on %s failed: %s" % (apk, device, output[-500:]),
                            details={"device": device, "exit": installed.returncode})
    stopped = _adb(adb, device, ["shell", "am", "force-stop", package], environ, log)
    if stopped.returncode != 0:
        raise ScaffoldError("LAUNCH_FAILED", "Stopping %s on %s failed, so it was not restarted: %s" % (
            package, device, stopped.stderr.strip()[-300:] or "exit %d" % stopped.returncode),
            details={"device": device, "package": package, "exit": stopped.returncode})
    launched = _adb(adb, device, ["shell", "monkey", "-p", package, "1"], environ, log)
    if launched.returncode != 0:
        raise ScaffoldError("LAUNCH_FAILED", "Launching %s on %s failed." % (package, device),
                            details={"device": device, "stderr": launched.stderr.strip()[-500:]})
    deadline = time.monotonic() + verify_seconds
    while True:
        probe = _adb(adb, device, ["shell", "pidof", package], environ, log, timeout=30)
        pid = probe.stdout.strip()
        if probe.returncode == 0 and pid:
            return {"device": device, "package": package, "pid": pid.split()[0]}
        if time.monotonic() >= deadline:
            raise ScaffoldError("LAUNCH_FAILED", "%s was launched on %s but no process appeared." % (package, device),
                                details={"device": device, "package": package})
        time.sleep(0.5)
