# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""macOS application outputs: naming, bundle validation, and restart."""

from __future__ import annotations

import os
import plistlib
import re
import signal
import time
from pathlib import Path

from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair

CHANNEL_SUFFIX = {"beta": " Beta", "dev": " Dev", "nightly": " Nightly"}
QUIT_WAIT_SECONDS = 15
TERM_WAIT_SECONDS = 10
KILL_WAIT_SECONDS = 5
LAUNCH_WAIT_SECONDS = 30


def app_name(configuration, channel=None):
    """Bundle name Core produces for the browser product."""
    if configuration == "Debug":
        return "Brave Browser Development.app"
    return "Brave Browser%s.app" % CHANNEL_SUFFIX.get(channel or "", "")


def app_path(output_dir, configuration, channel=None):
    return Path(output_dir) / app_name(configuration, channel)


def read_bundle(path):
    """Validate an application bundle and return its identity.

    Raises ARTIFACT_MISSING for an absent bundle and ARTIFACT_MISMATCH for one
    that is not a usable Brave browser application.
    """
    path = Path(path)
    if not path.is_dir():
        raise ScaffoldError("ARTIFACT_MISSING", "The application %s does not exist." % path,
                            details={"path": str(path)})
    try:
        info = plistlib.loads((path / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s has no readable Info.plist (%s)." % (path, error),
                            details={"path": str(path)})
    identifier = info.get("CFBundleIdentifier")
    executable = info.get("CFBundleExecutable")
    if not identifier or not executable:
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s is missing its bundle identifier or executable name." % path,
                            details={"path": str(path)})
    if not identifier.startswith("com.brave."):
        raise ScaffoldError("ARTIFACT_MISMATCH", "%s is not a Brave browser application (%s)." % (path, identifier),
                            details={"path": str(path), "bundle_identifier": identifier})
    binary = path / "Contents" / "MacOS" / executable
    if not os.access(binary, os.X_OK):
        raise ScaffoldError("ARTIFACT_MISMATCH", "The executable %s is missing or not executable." % binary,
                            details={"path": str(path)})
    return {"path": str(path), "kind": "app", "bundle_identifier": identifier, "executable": str(binary),
            "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or path.stem,
            "version": info.get("CFBundleShortVersionString")}


# --- restart -------------------------------------------------------------------


def running_instances(bundle, environ, log=None, poll=False):
    """Main processes of the application with the same bundle identifier, from any checkout."""
    result = run_capture(["ps", "-axo", "pid=,command="], os.getcwd(), environ, log, timeout=30, max_bytes=16 << 20,
                         poll=poll)
    if result.returncode != 0 or result.timed_out or result.truncated:
        why = ("was too large to read completely" if result.truncated else "timed out" if result.timed_out
               else "failed (exit %d)" % result.returncode)
        raise ScaffoldError("LAUNCH_FAILED", "The process listing %s, so running instances of the application "
                            "cannot be identified; nothing was stopped or launched." % why,
                            details={"exit": result.returncode, "stderr": result.stderr.strip()[-300:]})
    found = []
    pattern = re.compile(r"^\s*(\d+)\s+(.*?\.app)/Contents/MacOS/(\S.*)$")
    for line in result.stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        pid, bundle_path, executable = int(match.group(1)), match.group(2), match.group(3)
        try:
            info = plistlib.loads((Path(bundle_path) / "Contents" / "Info.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        name = info.get("CFBundleExecutable") or ""
        if info.get("CFBundleIdentifier") == bundle["bundle_identifier"] and \
                (executable == name or executable.startswith(name + " ")):
            found.append({"pid": pid, "bundle": bundle_path})
    return found


def _alive(pid, log=None):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    probe = run_capture(["ps", "-o", "stat=", "-p", str(pid)], os.getcwd(), None, log, timeout=10, poll=True)
    state = probe.stdout.strip()
    if probe.returncode == 0 and not probe.timed_out and not probe.truncated and state:
        return not state.startswith("Z")
    # The probe gave no usable answer, so only the process disappearing proves it is gone.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pids, seconds, log=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_alive(pid, log) for pid in pids):
            return True
        time.sleep(0.2)
    return not any(_alive(pid, log) for pid in pids)


def stop_instances(bundle, instances, environ, log=None):
    """Quit gracefully, then terminate, then kill; confirm exit. Returns the steps taken."""
    try:
        return _stop_instances(bundle, instances, environ, log)
    finally:
        if log is not None:
            log.finish_polls()


def _stop_instances(bundle, instances, environ, log):
    pids = [item["pid"] for item in instances]
    steps = []
    run_capture(["osascript", "-e", 'tell application id "%s" to quit' % bundle["bundle_identifier"]],
                os.getcwd(), environ, log, timeout=QUIT_WAIT_SECONDS)
    steps.append("quit")
    if _wait_gone(pids, QUIT_WAIT_SECONDS, log):
        return steps
    for name, sig, wait in (("terminate", signal.SIGTERM, TERM_WAIT_SECONDS), ("kill", signal.SIGKILL, KILL_WAIT_SECONDS)):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        steps.append(name)
        if _wait_gone(pids, wait, log):
            return steps
    raise ScaffoldError("LAUNCH_FAILED", "Could not stop the running %s (process %s)." % (
        bundle["name"], ", ".join(str(p) for p in pids)), details={"pids": pids, "steps": steps})


def launch(bundle, environ, log=None):
    """Open the selected bundle and confirm that its own process started."""
    try:
        return _launch(bundle, environ, log)
    finally:
        if log is not None:
            log.finish_polls()


def _launch(bundle, environ, log):
    result = run_capture(["open", bundle["path"]], os.getcwd(), environ, log, timeout=60)
    if result.returncode != 0:
        raise ScaffoldError("LAUNCH_FAILED", "Launching %s failed: %s" % (bundle["path"], result.stderr.strip()),
                            details={"exit": result.returncode})
    deadline = time.monotonic() + LAUNCH_WAIT_SECONDS
    while time.monotonic() < deadline:
        for item in running_instances(bundle, environ, log, poll=True):
            if os.path.realpath(item["bundle"]) == os.path.realpath(bundle["path"]):
                return item["pid"]
        time.sleep(0.3)
    raise ScaffoldError("LAUNCH_FAILED", "%s was opened but its process did not appear." % bundle["path"],
                        details={"path": bundle["path"]},
                        repairs=[repair(["open", bundle["path"]], note="Try launching it yourself.")])


def restart(bundle, environ, log=None):
    """Stop every running instance of the application, then launch the selected bundle."""
    instances = running_instances(bundle, environ, log)
    steps = stop_instances(bundle, instances, environ, log) if instances else []
    pid = launch(bundle, environ, log)
    return {"stopped": [item["pid"] for item in instances], "stop_steps": steps, "launched_pid": pid}
