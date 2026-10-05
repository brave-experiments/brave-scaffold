# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only iOS Simulator readiness checks."""

from __future__ import annotations

import os

from ..common.checks import BLOCKER, NOT_CHECKED, PASS, make_check
from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair
from . import ios
from . import sync as sync_module

AFFECTS = ("ios build", "ios run")


def machine_checks(ctx, scope):
    """Xcode and the simulator SDK on this machine."""
    version = run_capture(["xcodebuild", "-version"], os.getcwd(), ctx.environ, ctx.log, timeout=60)
    first = version.stdout.strip().replace("\n", "; ")
    checks = [make_check("ios-xcodebuild", PASS if version.returncode == 0 else BLOCKER,
                         first if version.returncode == 0 else "xcodebuild is unavailable (%s)." % (
                             version.stderr.strip()[-200:] or "install Xcode and select it with xcode-select"),
                         scope, affects=AFFECTS)]
    sdk = run_capture(["xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"], os.getcwd(), ctx.environ,
                      ctx.log, timeout=60)
    checks.append(make_check("ios-simulator-sdk", PASS if sdk.returncode == 0 and sdk.stdout.strip() else BLOCKER,
                             "iPhoneSimulator SDK %s" % sdk.stdout.strip() if sdk.returncode == 0 else
                             "The iPhoneSimulator SDK is unavailable (%s)." % sdk.stderr.strip()[-200:],
                             scope, affects=AFFECTS))
    return checks


def _selected(ctx):
    try:
        return ctx.identity(required=True, validate=False), None
    except ScaffoldError as error:
        if error.code in ("CHECKOUT_REQUIRED", "CHECKOUT_AMBIGUOUS"):
            return None, error
        raise


def build_checks(ctx, scope, remote_required=False):
    """The checkout's iOS target, project, and bootstrap, and a simulator the project can run on."""
    identity, error = _selected(ctx)
    if identity is None:
        return [make_check(name, NOT_CHECKED, error.message, scope, affects=AFFECTS, repairs=error.repairs,
                           **error.details)
                for name in ("ios-gclient-target", "ios-project", "ios-bootstrap", "ios-simulator")]
    targets = sync_module.gclient_targets(identity)
    ok = targets is not None and "ios" in targets
    checks = [make_check(
        "ios-gclient-target", PASS if ok else BLOCKER,
        "target_os includes ios." if ok else "This checkout is not configured for iOS (target_os: %s)." % (
            ", ".join(targets) if targets else "missing or unreadable"), scope, affects=AFFECTS,
        repairs=[] if ok else [repair(["bdev", "sync", "ios", "--checkout", str(identity.core)],
                                      note="Adds ios to the checkout's target_os and syncs; changes the checkout.")],
        target_os=targets)]
    project = ios.project_path(identity)
    pbxproj = project / "project.pbxproj"
    minimum = ios.deployment_target(pbxproj) if pbxproj.is_file() else None
    if minimum:
        checks.append(make_check("ios-project", PASS, "%s (deployment target iOS %s)" % (project, minimum), scope,
                                 affects=AFFECTS, project=str(project), deployment_target=minimum))
    else:
        checks.append(make_check("ios-project", BLOCKER, "Client.xcodeproj is missing or has no deployment target at %s."
                                 % project, scope, affects=AFFECTS, project=str(project)))
    missing = ios.missing_bootstrap_artifacts(identity)
    checks.append(make_check(
        "ios-bootstrap", PASS if not missing else BLOCKER,
        "Core's iOS bootstrap files are present." if not missing else
        "Core's iOS bootstrap files are missing (%d, for example %s)." % (len(missing), missing[0]),
        scope, affects=AFFECTS, missing=[str(path) for path in missing],
        repairs=[] if not missing else [ios.bootstrap_repair(identity)]))
    try:
        _, devices = ios.list_simulators(ctx.environ, ctx.log)
        usable = ios.compatible(devices, minimum)
        checks.append(make_check(
            "ios-simulator", PASS if usable else BLOCKER,
            "%d available simulator(s)%s." % (len(usable), " for iOS %s or later" % minimum if minimum else "")
            if usable else "No available iOS Simulator%s. Install a runtime in Xcode (Settings > Components)." % (
                " supports iOS %s or later" % minimum if minimum else ""),
            scope, affects=AFFECTS, simulators=[{"udid": d["udid"], "name": d["name"], "version": d["version"]}
                                                for d in usable[:20]]))
    except ScaffoldError as problem:
        checks.append(make_check("ios-simulator", BLOCKER, problem.message, scope, affects=AFFECTS, **problem.details))
    return checks

