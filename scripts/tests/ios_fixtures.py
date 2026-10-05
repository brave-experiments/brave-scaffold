# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fake Xcode tools and an iOS project layout for iOS tests. Nothing here needs Xcode."""

import json
from pathlib import Path

from tests.support import write_executable

RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-18-0"
OLD_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-15-0"


def simulator_json(booted=False, old_only=False):
    runtimes = [{"identifier": RUNTIME, "version": "18.0", "name": "iOS 18.0", "isAvailable": True},
                {"identifier": OLD_RUNTIME, "version": "15.0", "name": "iOS 15.0", "isAvailable": True}]
    devices = {RUNTIME: [
        {"udid": "AAAA-IPAD", "name": "iPad Pro", "state": "Shutdown", "isAvailable": True},
        {"udid": "BBBB-PHONE", "name": "iPhone 16", "state": "Booted" if booted else "Shutdown", "isAvailable": True},
        {"udid": "CCCC-PHONE", "name": "iPhone 16 Pro", "state": "Shutdown", "isAvailable": True},
        {"udid": "DDDD-GONE", "name": "iPhone 14", "state": "Shutdown", "isAvailable": False}],
        OLD_RUNTIME: [{"udid": "EEEE-OLD", "name": "iPhone 8", "state": "Shutdown", "isAvailable": True}]}
    if old_only:
        runtimes, devices = runtimes[1:], {OLD_RUNTIME: devices[OLD_RUNTIME]}
    return json.dumps({"runtimes": runtimes, "devices": devices})


FAKE_XCRUN = """#!%(python)s
import json, os, sys
args = sys.argv[1:]
record = os.environ.get("FAKE_RECORD")
def note():
    if record:
        with open(record, "a") as stream:
            stream.write(json.dumps({"tool": "xcrun", "argv": args, "cwd": os.getcwd()}) + "\\n")
if args[:2] == ["simctl", "list"]:
    if os.environ.get("FAKE_SIMCTL_FAIL"):
        print("simctl failed", file=sys.stderr)
        sys.exit(1)
    print(os.environ["FAKE_SIMCTL_JSON"])
elif args[:1] == ["simctl"]:
    note()
    if os.environ.get("FAKE_SIMCTL_FAIL_" + args[1].upper()):
        print("simctl %%s failed" %% args[1], file=sys.stderr)
        sys.exit(1)
    if args[1] == "launch":
        print("%%s: 4321" %% args[-1])
elif args[:1] == ["--sdk"] and "--show-sdk-version" in args:
    if os.environ.get("FAKE_NO_SIMULATOR_SDK"):
        sys.exit(1)
    print("18.0")
else:
    print("15.0")
"""

FAKE_XCODEBUILD = """#!%(python)s
import json, os, sys
args = sys.argv[1:]
record = os.environ.get("FAKE_RECORD")
if record and args != ["-version"]:
    with open(record, "a") as stream:
        stream.write(json.dumps({"tool": "xcodebuild", "argv": args, "cwd": os.getcwd(),
                                 "env": sorted(os.environ)}) + "\\n")
if args == ["-version"]:
    if os.environ.get("FAKE_NO_XCODEBUILD"):
        print("xcodebuild: error: tool requires Xcode", file=sys.stderr)
        sys.exit(1)
    print("Xcode 26.0\\nBuild version 17A1")
    sys.exit(0)
exec(open(os.environ["FAKE_XCODEBUILD_HOOK"]).read()) if os.environ.get("FAKE_XCODEBUILD_HOOK") else None
"""


def install_fake_xcode(sandbox):
    write_executable(sandbox.bin / "xcrun", FAKE_XCRUN)
    write_executable(sandbox.bin / "xcodebuild", FAKE_XCODEBUILD)


def make_ios_project(core, deployment_target="17.0", bootstrapped=True):
    """The parts of Core's iOS project the scaffold reads, and (optionally) what `ios_bootstrap` creates."""
    core = Path(core)
    app = core / "ios" / "brave-ios" / "App"
    (app / "Client.xcodeproj").mkdir(parents=True)
    (app / "Client.xcodeproj" / "project.pbxproj").write_text(
        "IPHONEOS_DEPLOYMENT_TARGET = %s;\nIPHONEOS_DEPLOYMENT_TARGET = 16.0;\n" % deployment_target)
    if bootstrapped:
        bootstrap(core)


def bootstrap(core):
    core = Path(core)
    link = core.parent / "out" / "ios_current_link"
    for name in ("BraveCore", "NalaAssets", "PartitionAllocSupport"):
        (link / (name + ".xcframework")).mkdir(parents=True, exist_ok=True)
        (link / (name + ".xcframework") / "Info.plist").write_text("<plist/>")
    (link / "args.xcconfig").write_text("")
    configuration = core / "ios" / "brave-ios" / "App" / "Configuration"
    configuration.mkdir(parents=True, exist_ok=True)
    (configuration / "LLDBInit").write_text("")


# Runs inside the fake xcodebuild. It does what Core's Debug scheme and Xcode do for a build: Core's pre-action
# produces the GN output and repoints ios_current_link, then Xcode produces Client.app.
BUILD_HOOK = """
import plistlib, shutil, stat
if os.environ.get("FAKE_XCODEBUILD_EXIT"):
    derived = args[args.index("-derivedDataPath") + 1] if "-derivedDataPath" in args else None
    if derived and not os.environ.get("FAKE_NO_PARTIAL"):
        os.makedirs(os.path.join(derived, "partial"), exist_ok=True)
    sys.exit(int(os.environ["FAKE_XCODEBUILD_EXIT"]))
if "build" not in args:
    sys.exit(0)
src = os.path.dirname(os.getcwd())
gn = os.path.join(src, "out", os.environ.get("FAKE_GN_NAME", "ios_Debug_arm64_simulator"))
os.makedirs(os.path.join(gn, "BraveCore.xcframework"), exist_ok=True)
for name in ("BraveCore", "NalaAssets", "PartitionAllocSupport"):
    os.makedirs(os.path.join(gn, name + ".xcframework"), exist_ok=True)
    open(os.path.join(gn, name + ".xcframework", "Info.plist"), "w").write("<plist/>")
open(os.path.join(gn, "args.xcconfig"), "w").write("")
link = os.path.join(src, "out", "ios_current_link")
if os.path.islink(link):
    os.unlink(link)
elif os.path.isdir(link):
    shutil.rmtree(link)
os.symlink(gn, link)
derived = args[args.index("-derivedDataPath") + 1] if "-derivedDataPath" in args else os.path.join(src, "out", "unexpected")
if not os.environ.get("FAKE_NO_APP"):
    app = os.path.join(derived, "Build", "Products", "Debug-iphonesimulator", "Client.app")
    os.makedirs(app, exist_ok=True)
    info = {"CFBundleIdentifier": os.environ.get("FAKE_IOS_BUNDLE_ID", "com.brave.ios.browser.dev"),
            "CFBundleExecutable": "Client", "CFBundleName": "Brave",
            "CFBundleSupportedPlatforms": [os.environ.get("FAKE_IOS_PLATFORM", "iPhoneSimulator")]}
    with open(os.path.join(app, "Info.plist"), "wb") as stream:
        plistlib.dump(info, stream)
    open(os.path.join(app, "Client"), "w").write("#!/bin/sh\\n")
    os.chmod(os.path.join(app, "Client"), 0o755)
"""


def install_build_hook(sandbox):
    path = sandbox.root / "xcodebuild_hook.py"
    path.write_text(BUILD_HOOK)
    return str(path)
