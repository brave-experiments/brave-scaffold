# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Targets, configurations, and the capability table."""

from __future__ import annotations

import platform
import sys

from .results import ScaffoldError, repair

TARGET_ALIASES = {"mac": "mac", "macos": "mac", "android": "android", "ios": "ios"}
RECOGNIZED_TARGETS = tuple(TARGET_ALIASES)
INITIAL_TARGETS = ("mac", "android")
CONFIGURATIONS = ("debug", "release")

SUPPORTED, LIMITED, UNVERIFIED, EXPERIMENTAL, UNSUPPORTED = (
    "supported", "limited", "unverified", "experimental", "unsupported")


def host_platform():
    return {"darwin": "mac"}.get(sys.platform)


def host_architecture():
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else machine


def normalize_target(token):
    return TARGET_ALIASES.get(token.lower()) if token else None


def effective_target(explicit, config):
    """Explicit target, then configured platform, then the host platform."""
    if explicit:
        target, source = normalize_target(explicit), "explicit"
    elif config.default_platform:
        target, source = normalize_target(config.default_platform), "configured"
    else:
        target, source = host_platform(), "host"
    if target is None:
        raise ScaffoldError(
            "UNSUPPORTED_CAPABILITY",
            "This host (%s) has no supported default target; name one explicitly." % sys.platform,
            details={"supported_targets": list(INITIAL_TARGETS)},
            repairs=[repair(["bdev", "capabilities"])])
    if target == "ios":
        raise ScaffoldError(
            "UNSUPPORTED_CAPABILITY", "iOS is not available in this release; use mac or android.",
            details={"supported_targets": list(INITIAL_TARGETS)})
    return target, source


# Combinations proven on a real checkout. A combination absent from this set is
# reported as unverified, never as supported.
VALIDATED = set()
# Operations whose commands exist in this release.
AVAILABLE_OPERATIONS = set()


def capability_table():
    """Operations by host, target, architecture, and configuration with status."""
    rows = []

    def add(target, operation, configuration, architecture, intended, note="", host="mac-arm64"):
        key = (target, operation, configuration, architecture)
        status = intended
        if intended in (SUPPORTED, LIMITED) and operation not in AVAILABLE_OPERATIONS and operation != "any":
            status, note = UNVERIFIED, "The command is not available in this release."
        elif intended == SUPPORTED and key not in VALIDATED:
            status, note = UNVERIFIED, (note + " Not yet validated on a real checkout.").strip()
        rows.append({"host": host, "target": target, "operation": operation,
                     "configuration": configuration, "architecture": architecture,
                     "status": status, "note": note})

    for operation in ("sync", "build", "run", "clean", "drift", "patches-update"):
        add("mac", operation, "debug", "arm64", SUPPORTED)
    add("mac", "test", "debug", "arm64", SUPPORTED, "Explicit suite required.")
    for operation in ("build", "run", "clean"):
        add("mac", operation, "release", "arm64", LIMITED, "Not part of the validated workflow.")
        add("mac", operation, "debug", "x64", LIMITED, "Not part of the validated workflow.")
    for operation in ("sync", "build", "deploy", "run", "clean"):
        add("android", operation, "debug", "arm64", SUPPORTED, "Debug arm64 APK." if operation == "build" else "")
    add("android", "build", "release", "arm64", LIMITED, "Not part of the validated workflow.")
    add("android", "test", "debug", "arm64", UNSUPPORTED, "Android tests are not available.")
    add("ios", "build", "debug", "arm64", UNSUPPORTED, "iOS is not available.")
    for target in ("mac", "android"):
        add(target, "any", "debug", "arm64", UNSUPPORTED,
            "Native Linux and Windows hosts are not available.", host="linux-or-windows")
    return rows
