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
INITIAL_TARGETS = ("mac", "android", "ios")
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
        if target is None:
            raise ScaffoldError(
                "INVALID_INPUT", "%r is not a target. Use mac, android, or ios; choose the configuration with "
                "--configuration debug|release." % explicit, details={"targets": list(INITIAL_TARGETS)})
    elif config.default_platform:
        target, source = normalize_target(config.default_platform), "configured"
    else:
        target, source = host_platform(), "host"
    if target is None:
        raise ScaffoldError(
            "UNSUPPORTED_CAPABILITY",
            "This host (%s) has no supported default target; name one explicitly." % sys.platform,
            details={"supported_targets": list(INITIAL_TARGETS)},
            repairs=[repair(["bcore", "capabilities"])])
    return target, source


# Combinations proven on a real checkout. A combination absent from this set is
# reported as unverified, never as supported.
VALIDATED = {("mac", operation, "debug", "arm64") for operation in ("sync", "build", "test", "run", "drift")} | {
    ("android", operation, "debug", "arm64") for operation in ("sync", "build", "test", "run", "deploy")} | {
    ("ios", operation, "debug", "arm64") for operation in ("sync", "build", "run")}
# (target, operation) pairs whose commands exist in this release.
AVAILABLE_OPERATIONS = {("mac", operation) for operation in
                        ("sync", "build", "test", "run", "clean", "drift", "patches-update")} | {
    ("android", operation) for operation in ("sync", "build", "test", "deploy", "run", "clean")} | {
    ("ios", operation) for operation in ("sync", "build", "run", "clean")}


def capability_table():
    """Operations by host, target, architecture, and configuration with status."""
    rows = []

    def add(target, operation, configuration, architecture, intended, note="", host="mac-arm64"):
        key = (target, operation, configuration, architecture)
        status = intended
        if intended in (SUPPORTED, LIMITED) and (target, operation) not in AVAILABLE_OPERATIONS and operation != "any":
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
    add("android", "test", "debug", "arm64", SUPPORTED,
        "brave_junit_tests (host) and brave_java_unit_tests (device); needs the required Android test support branch.")
    add("ios", "sync", "debug", "arm64", SUPPORTED, "Adds ios to target_os; Core's hooks bootstrap the project.")
    for operation in ("build", "run", "clean"):
        add("ios", operation, "debug", "arm64", SUPPORTED, "iOS Simulator only.")
    add("ios", "any", "release", "arm64", UNSUPPORTED, "Only Debug simulator builds are available.")
    for target in ("mac", "android"):
        add(target, "any", "debug", "arm64", UNSUPPORTED,
            "Native Linux and Windows hosts are not available.", host="linux-or-windows")
    return rows
