# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only Android readiness checks."""

from __future__ import annotations

import os

from ..common.checks import BLOCKER, NOT_CHECKED, PASS, WARNING, make_check
from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair
from . import adb, android_deps, cmd_build, rbe_checks

UNCHECKED_NAMES = ("android-gclient-target", "android-support-working-copy", "android-support-compatibility",
                   "android-support-currency")


def machine_checks(ctx, scope):
    """Tools on this machine. adb matters for install and run, not for compiling."""
    path = adb.find_adb(ctx.environ)
    checks = [make_check("adb", PASS if path else BLOCKER,
                         path or "adb was not found (set ANDROID_HOME or put adb on PATH).", scope,
                         affects=("android run", "android deploy"), path=path)]
    lfs = run_capture(["git", "lfs", "version"], os.getcwd(), ctx.environ, ctx.log, timeout=30)
    checks.append(make_check(
        "git-lfs", PASS if lfs.returncode == 0 else WARNING,
        "git-lfs is available." if lfs.returncode == 0 else
        "git-lfs is missing; the support repository's resources are stored with it.", scope, required=False,
        affects=("android setup",), repairs=[] if lfs.returncode == 0 else
        [repair(["brew", "install", "git-lfs"], requires_user_action=True)]))
    return checks


def _selected(ctx):
    try:
        return ctx.identity(required=True, validate=False), None
    except ScaffoldError as error:
        if error.code in ("CHECKOUT_REQUIRED", "CHECKOUT_AMBIGUOUS"):
            return None, error
        raise


def build_checks(ctx, scope):
    """What a build needs before preparation: an Android target in the checkout and RBE configuration."""
    identity, error = _selected(ctx)
    if identity is None:
        return [make_check("android-gclient-target", NOT_CHECKED, "No checkout is selected: %s" % error.message,
                           scope, affects=("android build",), repairs=error.repairs, **error.details)]
    targets = cmd_build.gclient_targets(identity)
    ok = targets is not None and "android" in targets
    sync = repair(["bdev", "sync", "android", "--checkout", str(identity.core)],
                  note="Adds android to the checkout's target_os and syncs; changes the checkout.")
    checks = [make_check("android-gclient-target", PASS if ok else BLOCKER,
                         "target_os includes android." if ok else
                         "This checkout is not configured for Android (target_os: %s)." % (
                             ", ".join(targets) if targets else "missing or unreadable"),
                         scope, affects=("android build",), repairs=[] if ok else [sync], target_os=targets)]
    return checks + rbe_checks._rbe_config(ctx, identity, scope, required=False)


def support_checks(ctx, scope):
    """The per-checkout support working copy, its compatibility gate, and whether it is applied."""
    identity, error = _selected(ctx)
    if identity is None:
        return [make_check(name, NOT_CHECKED, "No checkout is selected: %s" % error.message, scope,
                           affects=("android build",), repairs=error.repairs) for name in UNCHECKED_NAMES[1:]]
    wc = android_deps.working_copy(identity)
    facts = android_deps.inspect_working_copy(wc, ctx.log)
    setup = repair(["bdev", "android", "setup", "--checkout", str(identity.core)],
                   note="Explicit preparation; clones the support repository (network) for this checkout only.")
    if facts is None:
        return [make_check("android-support-working-copy", BLOCKER, "No support working copy at %s." % wc, scope,
                           affects=("android build",), repairs=[setup])] + [
            make_check(name, NOT_CHECKED, "Needs the working copy.", scope, affects=("android build",))
            for name in UNCHECKED_NAMES[2:]]
    checks = [make_check("android-support-working-copy", PASS, "%s at %s%s" % (
        facts["branch"] or "detached", (facts["head"] or "")[:12],
        " (local changes present)" if facts["dirty"] else ""), scope, affects=("android build",),
        working_copy=facts)]
    ok, detail = android_deps.run_gate(wc, "copyMacRes.sh", ctx.environ, ctx.log)
    checks.append(make_check("android-support-compatibility", PASS if ok else BLOCKER,
                             "The support revision's version gates accept this checkout." if ok else
                             "Incompatible with this checkout: %s" % detail, scope, affects=("android build",),
                             repairs=[] if ok else [repair(["git", "-C", str(wc), "log", "--oneline", "-n", "10"])]))
    current, why = android_deps.resources_current(identity, wc) if ok else (False, "not evaluated")
    checks.append(make_check("android-support-currency", PASS if current else WARNING,
                             "Support resources are copied and current." if current else
                             "Support resources will be refreshed by the next build: %s" % why,
                             scope, required=False, affects=("android build",)))
    return checks
