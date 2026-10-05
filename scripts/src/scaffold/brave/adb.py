# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android device discovery, selection, and app restart through adb."""

from __future__ import annotations

import os
import shutil
import sys
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
            "DEVICE_AMBIGUOUS", "More than one device is available. Add one of these options to your command.",
            details={"devices": _listing(devices), "example": "--device %s" % usable[0]["id"],
                     "device_options": [["--device", item["id"]] for item in usable]})
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


def device_label(adb, device, environ, log=None):
    model = _adb(adb, device["id"], ["shell", "getprop", "ro.product.model"], environ, log, timeout=10)
    name = model.stdout.strip().replace("\n", " ") if model.returncode == 0 else ""
    kind = "emulator" if device["id"].startswith("emulator-") else "physical device"
    if kind == "emulator":
        avd = _adb(adb, device["id"], ["emu", "avd", "name"], environ, log, timeout=10)
        if avd.returncode == 0:
            names = [line for line in avd.stdout.splitlines() if line and line != "OK"]
            if names:
                name = names[0]
    return "%s (%s) - %s" % (name or "Android device", kind, device["id"])


def pick_device(adb, devices, environ, log=None, stdin=None, stderr=None, allow_all=False):
    """Choose a device, or return None for an explicitly offered All option."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    usable = [device for device in devices if device["state"] == "device"]
    print("Choose an Android device:", file=stderr)
    for number, device in enumerate(usable, 1):
        print("  %d. %s" % (number, device_label(adb, device, environ, log)), file=stderr)
    if allow_all:
        print("  a. All compatible devices", file=stderr)
    while True:
        prompt = "Choose [1-%d%s] (Enter for 1): " % (len(usable), ", a" if allow_all else "")
        print(prompt, end="", file=stderr, flush=True)
        answer = stdin.readline()
        if answer == "":
            raise ScaffoldError("INVALID_INPUT", "Device selection cancelled; nothing was built or deployed.")
        answer = answer.strip()
        if not answer:
            return usable[0]
        if allow_all and answer.lower() == "a":
            return None
        if answer.isdecimal() and 1 <= int(answer) <= len(usable):
            return usable[int(answer) - 1]
        print("Enter a number from 1 to %d%s." % (len(usable), " or a for All" if allow_all else ""), file=stderr)


def device_capabilities(adb, device, environ, log=None):
    properties = {}
    for key, prop in (("abis", "ro.product.cpu.abilist"), ("sdk", "ro.build.version.sdk")):
        result = _adb(adb, device["id"], ["shell", "getprop", prop], environ, log, timeout=10)
        if result.returncode != 0 or not result.stdout.strip():
            raise ScaffoldError("DEVICE_UNAVAILABLE", "Could not read %s from %s." % (prop, device["id"]))
        properties[key] = result.stdout.strip()
    if not properties["sdk"].isdecimal():
        raise ScaffoldError("DEVICE_UNAVAILABLE", "Could not read the Android version from %s." % device["id"])
    return {**device, "abis": properties["abis"].split(","), "sdk": int(properties["sdk"])}


def _adb(adb, device, args, environ, log, timeout=120):
    return run_capture([adb, "-s", device, *args], os.getcwd(), environ, log, timeout=timeout)


def restart_package(adb, device, apk, package, environ, log=None, verify_seconds=VERIFY_SECONDS,
                    progress=None, started=None, failed=None):
    """Install over the existing app (data is kept), stop only this package, launch, verify.

    Callbacks record only actual starts and observed outcomes. Launch succeeds
    only after PID confirmation; its command and verification exits are separate.
    """
    progress = progress or (lambda name, **outcome: None)
    started = started or (lambda name: None)
    failed = failed or (lambda name, **outcome: None)
    started("install-apk")
    installed = _adb(adb, device, ["install", "-d", "-r", "-g", apk], environ, log, timeout=600)
    if installed.returncode != 0 or "Success" not in installed.stdout:
        failed("install-apk", exit=installed.returncode)
        output = (installed.stdout + installed.stderr).strip()
        raise ScaffoldError("LAUNCH_FAILED", "Installing %s on %s failed: %s" % (apk, device, output[-500:]),
                            details={"device": device, "phase": "install-apk", "exit": installed.returncode},
                            child_exit_code=installed.returncode)
    progress("install-apk", exit=installed.returncode)
    started("stop-package")
    stopped = _adb(adb, device, ["shell", "am", "force-stop", package], environ, log)
    if stopped.returncode != 0:
        failed("stop-package", exit=stopped.returncode)
        raise ScaffoldError("LAUNCH_FAILED", "Stopping %s on %s failed, so it was not restarted: %s" % (
            package, device, stopped.stderr.strip()[-300:] or "exit %d" % stopped.returncode),
            details={"device": device, "package": package, "phase": "stop-package", "exit": stopped.returncode},
            child_exit_code=stopped.returncode)
    progress("stop-package", exit=stopped.returncode)
    started("launch-package")
    launched = _adb(adb, device, ["shell", "monkey", "-p", package, "1"], environ, log)
    if launched.returncode != 0:
        failed("launch-package", exit=launched.returncode)
        raise ScaffoldError("LAUNCH_FAILED", "Launching %s on %s failed." % (package, device),
                            details={"device": device, "phase": "launch-package", "exit": launched.returncode,
                                     "stderr": launched.stderr.strip()[-500:]}, child_exit_code=launched.returncode)
    deadline = time.monotonic() + verify_seconds
    while True:
        probe = _adb(adb, device, ["shell", "pidof", package], environ, log, timeout=30)
        pid = probe.stdout.strip()
        if probe.returncode == 0 and pid:
            progress("launch-package", exit=launched.returncode, verification_exit=probe.returncode,
                     pid=pid.split()[0])
            return {"device": device, "package": package, "pid": pid.split()[0]}
        if time.monotonic() >= deadline:
            failed("launch-package", exit=launched.returncode, verification_exit=probe.returncode)
            raise ScaffoldError("LAUNCH_FAILED", "%s was launched on %s but no process appeared." % (package, device),
                                details={"device": device, "package": package, "phase": "launch-package",
                                         "exit": launched.returncode, "verification_exit": probe.returncode},
                                child_exit_code=launched.returncode)
        time.sleep(0.5)
