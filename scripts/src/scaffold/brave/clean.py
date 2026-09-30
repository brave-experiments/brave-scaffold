# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cleanup of generated build output owned by the selected checkout."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..common.cli import CommandSpec, Opt, Positional
from ..common.platforms import RECOGNIZED_TARGETS, effective_target
from ..common.results import EXIT_PARTIAL, Result, ScaffoldError

CONFIG_NAMES = {"debug": "Debug", "release": "Release"}
SUPPORTED_TARGETS = ("mac", "android")


@dataclass
class Entry:
    name: str
    path: Path
    size_kib: int | None = None
    outcome: str = "planned"
    detail: str | None = None


def _with_arch(name, base, arch):
    if arch:
        return name == base or name == "%s_%s" % (base, arch)
    return name == base or name.startswith(base + "_")


def matches(name, target, config, arch):
    """Whether a directory name under src/out is an output of this target and configuration."""
    label = CONFIG_NAMES[config]
    if target == "mac":
        return any(_with_arch(name, label + sku, arch) for sku in ("", "Origin"))
    bases = ("android_" + label, "android_tests_" + label, "android_%sOrigin" % label,
             "android_Origin_" + label)
    return any(_with_arch(name, base, arch) for base in bases)


def owned_out_dir(identity):
    """The checkout's own src/out, which must not be a symlink or resolve elsewhere."""
    out = identity.src / "out"
    if not out.is_dir():
        return None
    if out.is_symlink() or Path(os.path.realpath(out)) != Path(os.path.realpath(identity.src)) / "out":
        raise ScaffoldError("OWNERSHIP_CONFLICT", "%s is a symlink or resolves outside the checkout; "
                            "nothing was deleted." % out, details={"out": str(out)})
    return out


def check_entry(path, out_dir):
    """Return a reason the entry may not be deleted, or None. Re-run immediately before deleting."""
    if path.is_symlink():
        return "is a symlink"
    if not path.is_dir():
        return "is no longer a directory"
    if Path(os.path.realpath(path)).parent != Path(os.path.realpath(out_dir)):
        return "is not an immediate child of the checkout's src/out"
    if (path / ".git").exists():
        return "contains a .git entry"
    return None


def disk_usage_kib(path):
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_blocks // 2
            except OSError:
                pass
    return total


def format_kib(kib):
    if kib is None:
        return "unknown"
    value = float(kib)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return "%.0f %s" % (value, unit) if unit == "KiB" or value >= 100 else "%.1f %s" % (value, unit)
        value /= 1024


def plan_cleanup(out_dir, targets, configs, arch, sizes):
    entries = []
    for child in sorted(out_dir.iterdir()):
        if not any(matches(child.name, t, c, arch) for t in targets for c in configs):
            continue
        entry = Entry(child.name, Path(os.path.realpath(child)) if not child.is_symlink() else child)
        reason = check_entry(child, out_dir)
        if reason:
            entry.outcome, entry.detail = "skipped", reason
        elif sizes:
            entry.size_kib = disk_usage_kib(child)
        entries.append(entry)
    return entries


def execute_plan(entries, out_dir, before_delete=None):
    """Delete each still-valid entry, revalidating immediately before removing it."""
    for entry in entries:
        if entry.outcome != "planned":
            continue
        if before_delete:
            before_delete(entry)
        reason = check_entry(out_dir / entry.name, out_dir)
        if reason or Path(os.path.realpath(out_dir / entry.name)) != entry.path:
            entry.outcome, entry.detail = "skipped", reason or "changed after the plan was made"
            continue
        try:
            shutil.rmtree(out_dir / entry.name)
            entry.outcome = "deleted"
        except OSError as error:
            entry.outcome, entry.detail = "failed", str(error)


def run_clean(ctx):
    identity = ctx.identity()
    token = ctx.parsed.positionals[0].lower() if ctx.parsed.positionals else None
    if token not in (None, "all") and token not in RECOGNIZED_TARGETS:
        raise ScaffoldError("INVALID_INPUT", "Unknown clean target %r." % token,
                            details={"targets": [*SUPPORTED_TARGETS, "all"]})
    if token == "all":
        targets = list(SUPPORTED_TARGETS)
    else:
        target, _ = effective_target(token, ctx.config)
        targets = [target]
    config_choice = ctx.parsed.get("configuration", "all")
    configs = list(CONFIG_NAMES) if config_choice == "all" else [config_choice]
    arch = ctx.parsed.get("arch")
    execute = bool(ctx.parsed.get("execute"))
    out_dir = owned_out_dir(identity)
    entries = plan_cleanup(out_dir, targets, configs, arch, not ctx.parsed.get("no_size")) if out_dir else []
    if execute:
        execute_plan(entries, out_dir)
    total = None if ctx.parsed.get("no_size") else sum(e.size_kib or 0 for e in entries if e.size_kib is not None)
    data = {"mode": "execute" if execute else "preview", "targets": targets, "configurations": configs,
            "arch": arch, "out_dir": str(out_dir) if out_dir else None, "total_kib": total,
            "entries": [{"name": e.name, "path": str(e.path), "size_kib": e.size_kib, "outcome": e.outcome,
                         "detail": e.detail} for e in entries]}
    lines = ["%s in %s" % ("Deleting" if execute else "Preview (nothing is deleted)",
                           out_dir or identity.src / "out")]
    for entry in entries:
        lines.append("  %-8s %10s  %s%s" % (entry.outcome, format_kib(entry.size_kib), entry.path,
                                          "  (%s)" % entry.detail if entry.detail else ""))
    if not entries:
        lines.append("  No matching build output directories.")
    elif not execute:
        lines.append("Run again with --execute to delete the directories marked 'planned'.")
    result = Result(command="clean", data=data, text="\n".join(lines))
    if execute and any(e.outcome in ("skipped", "failed") for e in entries):
        result.status, result.exit_code = "partial", EXIT_PARTIAL
        result.add_warning("CLEAN_INCOMPLETE", "Some directories were not deleted; see the entries for reasons.")
    return result


SPEC = CommandSpec(
    "clean", "List the checkout's generated build outputs, or delete them with --execute.", run_clean,
    positionals=(Positional("target", help="mac, android, or all; omitted means the default target only."),),
    options=(Opt("--configuration", "configuration", choices=("debug", "release", "all"),
                 help="Configuration to match (default: all).", metavar="CONFIG"),
             Opt("--arch", "arch", help="Only this architecture suffix, such as arm64.", metavar="ARCH"),
             Opt("--execute", "execute", takes_value=False, help="Delete the listed directories."),
             Opt("--no-size", "no_size", takes_value=False, help="Skip size calculation.")),
    side_effects="Preview writes nothing. --execute deletes matching directories directly under the "
                 "selected checkout's src/out. Stop builds first; one operator per checkout.",
    examples=("bdev clean", "bdev clean android --configuration debug --arch arm64", "bdev clean all --execute"))
