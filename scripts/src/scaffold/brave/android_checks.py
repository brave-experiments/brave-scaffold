# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only Android readiness checks."""

from __future__ import annotations

import os
import shutil

from ..common.checks import BLOCKER, NOT_CHECKED, PASS, WARNING, make_check
from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair
from . import adb, android_deps, rbe_checks
from . import sync as sync_module

UNCHECKED_NAMES = ("android-gclient-target", "android-support-working-copy", "android-support-lfs",
                   "android-support-compatibility", "android-support-currency")


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
        affects=("android setup",), version=lfs.stdout.strip(),
        git=shutil.which("git", path=ctx.environ.get("PATH")),
        git_lfs=shutil.which("git-lfs", path=ctx.environ.get("PATH")), repairs=[] if lfs.returncode == 0 else
        [repair(["brew", "install", "git-lfs"], requires_user_action=True)]))
    return checks


def _selected(ctx):
    try:
        return ctx.identity(required=True, validate=False), None
    except ScaffoldError as error:
        if error.code in ("CHECKOUT_REQUIRED", "CHECKOUT_AMBIGUOUS"):
            return None, error
        raise


def build_checks(ctx, scope, remote_required=False):
    """What a build needs before preparation: an Android target in the checkout and, for remote compilation,
    local RBE configuration."""
    identity, error = _selected(ctx)
    if identity is None:
        return [make_check("android-gclient-target", NOT_CHECKED, error.message,
                           scope, affects=("android build",), repairs=error.repairs, **error.details)]
    targets = sync_module.gclient_targets(identity)
    ok = targets is not None and "android" in targets
    sync = repair(["bdev", "sync", "android", "--checkout", str(identity.core)],
                  note="Adds android to the checkout's target_os and syncs; changes the checkout.")
    checks = [make_check("android-gclient-target", PASS if ok else BLOCKER,
                         "target_os includes android." if ok else
                         "This checkout is not configured for Android (target_os: %s)." % (
                             ", ".join(targets) if targets else "missing or unreadable"),
                         scope, affects=("android build",), repairs=[] if ok else [sync], target_os=targets)]
    return checks + rbe_checks._rbe_config(ctx, identity, scope, required=remote_required)


def support_checks(ctx, scope):
    """The per-checkout support working copy, its compatibility gate, and whether it is applied."""
    identity, error = _selected(ctx)
    if identity is None:
        return [make_check(name, NOT_CHECKED, error.message, scope,
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
    try:
        pointers = android_deps.lfs_pointers(wc, ctx.log)
        checks.append(make_check(
            "android-support-lfs", PASS if not pointers else BLOCKER,
            "The support repository's large files are materialized." if not pointers else
            "%d large file(s) are still pointers, so resources cannot be copied (for example %s)." % (
                len(pointers), pointers[0]), scope, affects=("android build",), pointers=pointers[:20],
            repairs=[] if not pointers else [repair(
                ["bdev", "android", "setup", "--checkout", str(identity.core)],
                note="Explicit preparation: fetches the missing large files (network).")]))
    except ScaffoldError as error:
        checks.append(make_check("android-support-lfs", BLOCKER, error.message, scope, affects=("android build",),
                                 repairs=error.repairs))
    try:
        plan = android_deps.plan_preparation(ctx, identity, ctx.log)
    except ScaffoldError as error:
        checks.append(make_check("android-support-compatibility", BLOCKER, error.message, scope,
                                 affects=("android build",), repairs=error.repairs))
        checks.append(make_check("android-support-currency", NOT_CHECKED, "Needs compatible support scripts.",
                                 scope, affects=("android build",)))
        return checks
    checks.append(make_check("android-support-compatibility", PASS,
                             "The support revision's version gates accept this checkout.", scope,
                             affects=("android build",)))
    status = {"current": PASS, "refresh": WARNING, "conflict": BLOCKER}[plan.action]
    message = plan.reason if plan.action != "refresh" else (
        "The next build will refresh support automatically and may replace local files: " + plan.reason)
    checks.append(make_check("android-support-currency", status, message, scope,
                             required=plan.action == "conflict", affects=("android build",),
                             files=plan.conflicts))
    return checks
