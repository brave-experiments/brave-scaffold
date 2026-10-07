# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cleanup of generated build output owned by the selected checkout."""

from __future__ import annotations

import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from ..common.cli import CommandSpec, Opt, Positional
from ..common.platforms import RECOGNIZED_TARGETS, effective_target
from ..common.results import EXIT_PARTIAL, Cancelled, Result, ScaffoldError
from .records import all_operations, cleanup_remainders, track

CONFIG_NAMES = {"debug": "Debug", "release": "Release"}
SUPPORTED_TARGETS = ("mac", "android", "ios")
PRIVATE_PREFIX = ".scaffold-deleting-"


@dataclass
class Entry:
    name: str
    path: Path
    size_kib: int | None = None
    outcome: str = "planned"
    detail: str | None = None
    identity: tuple | None = None
    parent_identity: tuple | None = None
    original: str | None = None  # the output an interrupted deletion was removing, for a recorded remainder
    private: str | None = None  # the private name the directory was moved to once its deletion began


def _with_arch(name, base, arch):
    """Core names the x64 output without a suffix, so an unsuffixed directory belongs to x64 and no other arch."""
    if not arch:
        return name == base or name.startswith(base + "_")
    if arch == "x64":
        return name == base or name == base + "_x64"
    return name == "%s_%s" % (base, arch)


def matches(name, target, config, arch):
    """Whether a directory name under src/out is an output of this target and configuration."""
    label = CONFIG_NAMES[config]
    if target == "ios":
        found = re.fullmatch(r"ios_%s(?:_(arm64|x64))?(?:_simulator)?(?:_xcode_derived_data)?" % label, name)
        if not found or not arch:
            return bool(found)
        # Xcode's derived data holds products for every architecture, so a narrower selection leaves it alone.
        return not name.endswith("_xcode_derived_data") and (found.group(1) or "x64") == arch
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


def _identity(status):
    return (status.st_dev, status.st_ino)


def plan_cleanup(out_dir, targets, configs, arch, sizes, remnants=()):
    """Matching directories, each with the identity of the directory that was approved.

    `remnants` are the directories earlier interrupted deletions recorded (see `records.all_operations`). A
    directory with a cleanup name is a remainder only when a record names it and it is still the recorded
    directory; any other directory with that kind of name is reported and left alone.
    """
    entries = []
    parent = _identity(os.stat(out_dir))
    for child in sorted(out_dir.iterdir()):
        if child.name.startswith(PRIVATE_PREFIX):
            entries.append(_remainder_entry(child, out_dir, parent, targets, configs, arch, remnants))
            continue
        if not any(matches(child.name, t, c, arch) for t in targets for c in configs):
            continue
        entry = Entry(child.name, Path(os.path.realpath(child)) if not child.is_symlink() else child)
        reason = check_entry(child, out_dir)
        if reason:
            entry.outcome, entry.detail = "skipped", reason
        else:
            entry.identity, entry.parent_identity = _identity(os.lstat(child)), parent
            if sizes:
                entry.size_kib = disk_usage_kib(child)
        entries.append(entry)
    return entries


def _remainder_entry(child, out_dir, parent, targets, configs, arch, remnants):
    entry = Entry(child.name, child)
    if child.is_symlink() or not child.is_dir():
        entry.outcome, entry.detail = "skipped", "has a cleanup name but is not a directory; left alone"
        return entry
    identity = _identity(os.lstat(child))
    recorded = next((item for item in remnants if item["private"] == child.name and
                     tuple(item["identity"]) == identity and item["out_dir"] == str(out_dir)), None)
    if recorded is None:
        entry.outcome, entry.detail = "skipped", ("has a cleanup name that no interrupted deletion of this "
                                                   "checkout recorded, or is not the directory it recorded; left alone")
        return entry
    entry.original, entry.identity, entry.parent_identity = recorded["directory"], identity, parent
    describe = "unfinished deletion of %s (operation %s)" % (recorded["directory"], recorded["operation_id"])
    if any(matches(recorded["directory"], t, c, arch) for t in targets for c in configs):
        entry.detail = describe
    else:
        entry.outcome, entry.detail = "unselected", "%s; select its target to finish it" % describe
    return entry


def _remove_contents(directory_fd):
    """Delete everything below an open directory through descriptors; links are removed, never followed."""
    with os.scandir(directory_fd) as scanner:
        children = [(item.name, item.is_dir(follow_symlinks=False)) for item in scanner]
    for name, is_directory in children:
        if not is_directory:
            os.unlink(name, dir_fd=directory_fd)
            continue
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            _remove_contents(child_fd)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=directory_fd)


def _contains_repository(directory_fd):
    """A held directory's contents may have changed since planning or interruption."""
    try:
        os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _delete_approved(out_fd, out_dir, entry, during_delete=None, op=None):
    """Delete exactly the directory that was approved; return (outcome, detail).

    The name is checked against the planned identity, then the directory is held open and moved
    to a private name inside src/out. If the moved entry is not the approved one, it is put back and
    nothing is deleted, so a replacement is never reopened by name.
    """
    try:
        status = os.lstat(entry.name, dir_fd=out_fd)
    except FileNotFoundError:
        return "skipped", "no longer exists"
    if stat.S_ISLNK(status.st_mode):
        return "skipped", "is a symlink"
    if not stat.S_ISDIR(status.st_mode):
        return "skipped", "is no longer a directory"
    if _identity(status) != entry.identity:
        return "skipped", "changed after the plan was made"
    held = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=out_fd)
    try:
        if _identity(os.fstat(held)) != entry.identity:
            return "skipped", "changed after the plan was made"
        if _contains_repository(held):
            return "skipped", "contains a .git entry"
        if during_delete:
            during_delete(entry)
        private = "%s%s-%s" % (PRIVATE_PREFIX, entry.name, secrets.token_hex(4))
        _record_start(op, entry, private, out_dir)
        os.rename(entry.name, private, src_dir_fd=out_fd, dst_dir_fd=out_fd)
        entry.private = private
        if _identity(os.lstat(private, dir_fd=out_fd)) != entry.identity:
            os.rename(private, entry.name, src_dir_fd=out_fd, dst_dir_fd=out_fd)
            entry.private = None
            _record_end(op, False, directory=entry.name, reason="changed while it was being deleted")
            return "skipped", "changed while it was being deleted"
        return _remove_held(out_fd, held, private, op, entry.name)
    finally:
        os.close(held)


def _record_start(op, entry, private, out_dir):
    """Save what a later run needs to recognise the remainder, before the directory is moved or emptied."""
    if op is not None:
        op.start("delete", directory=entry.original or entry.name, private=private, out_dir=str(out_dir),
                 identity=list(entry.identity))


def _record_end(op, succeeded, **outcome):
    if op is not None:
        (op.succeed if succeeded else op.fail)("delete", **outcome)


def _remove_held(out_fd, held, private, op, directory):
    try:
        if _contains_repository(held):
            reason = "contains a .git entry; left as %s in src/out" % private
            _record_end(op, False, directory=directory, private=private, reason=reason)
            return "skipped", reason
        _remove_contents(held)
        os.rmdir(private, dir_fd=out_fd)
    except OSError as error:
        _record_end(op, False, directory=directory, private=private, reason=str(error))
        return "failed", "%s; what is left of the directory is %s in src/out" % (error, private)
    _record_end(op, True, directory=directory)
    return "deleted", None


def _finish_remainder(out_fd, out_dir, entry, op=None):
    """Continue an interrupted deletion of the directory a record names, if it is still exactly that directory."""
    try:
        status = os.lstat(entry.name, dir_fd=out_fd)
    except FileNotFoundError:
        return "skipped", "no longer exists"
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode) or _identity(status) != entry.identity:
        return "skipped", "is no longer the directory the interrupted deletion recorded"
    held = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=out_fd)
    try:
        if _identity(os.fstat(held)) != entry.identity:
            return "skipped", "is no longer the directory the interrupted deletion recorded"
        entry.private = entry.name
        _record_start(op, entry, entry.name, out_dir)
        return _remove_held(out_fd, held, entry.name, op, entry.original)
    finally:
        os.close(held)


def execute_plan(entries, out_dir, before_delete=None, during_delete=None, op=None):
    """Delete each entry that is still the approved directory inside the approved src/out.

    With an operation record, the private name and identity of a directory are saved before it is moved, and a
    cancellation leaves the entry `interrupted` with the remainder named.
    """
    out_fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for entry in entries:
            if entry.outcome != "planned":
                continue
            if before_delete:
                before_delete(entry)
            if _identity(os.fstat(out_fd)) != entry.parent_identity or _identity(os.stat(out_dir)) != \
                    entry.parent_identity:
                entry.outcome, entry.detail = "skipped", "src/out was replaced after the plan was made"
                continue
            try:
                if entry.original:
                    entry.outcome, entry.detail = _finish_remainder(out_fd, out_dir, entry, op)
                else:
                    entry.outcome, entry.detail = _delete_approved(out_fd, out_dir, entry, during_delete, op)
            except OSError as error:
                entry.outcome, entry.detail = "failed", str(error)
            except Cancelled:
                if entry.private:
                    entry.outcome, entry.detail = "interrupted", "cancelled; what is left of the directory is %s in " \
                                                                 "src/out" % entry.private
                raise
    finally:
        os.close(out_fd)


def run_clean(ctx):
    identity = ctx.identity()
    token = ctx.parsed.positionals[0].lower() if ctx.parsed.positionals else None
    if token not in (None, "all") and token not in RECOGNIZED_TARGETS:
        raise ScaffoldError("INVALID_INPUT", "Unknown clean target %r." % token,
                            details={"targets": [*SUPPORTED_TARGETS, "all"]})
    if token == "all":
        targets = list(SUPPORTED_TARGETS)
        target_source = "explicit"
    else:
        target, target_source = effective_target(token, ctx.config)
        targets = [target]
    config_choice = ctx.parsed.get("configuration", "all")
    configs = list(CONFIG_NAMES) if config_choice == "all" else [config_choice]
    arch = ctx.parsed.get("arch")
    execute = bool(ctx.parsed.get("execute"))
    out_dir = owned_out_dir(identity)
    remnants = recorded_remainders(identity, ctx.state_root)
    entries = plan_cleanup(out_dir, targets, configs, arch, not ctx.parsed.get("no_size"), remnants) if out_dir else []
    if execute and any(entry.outcome == "planned" for entry in entries):
        with track(ctx, "clean", identity, {"targets": targets, "configurations": configs, "arch": arch,
                                            "out_dir": str(out_dir)}) as op:
            try:
                execute_plan(entries, out_dir, op=op)
            finally:
                op.detail(**{outcome: [e.name for e in entries if e.outcome == outcome]
                             for outcome in ("deleted", "skipped", "failed", "interrupted")},
                          remaining=[e.private for e in entries if e.private and e.outcome in ("failed", "interrupted")])
            return op.complete(clean_result(ctx, identity, entries, out_dir, targets, configs, arch, execute,
                                            target_source))
    return clean_result(ctx, identity, entries, out_dir, targets, configs, arch, execute, target_source)


def recorded_remainders(identity, state_root):
    """Directories that earlier cleanups of this checkout recorded as moved aside and not fully removed."""
    found = []
    for record in all_operations(state_root, identity.core, "clean"):
        found.extend({**item, "operation_id": record["operation_id"]} for item in cleanup_remainders(record))
    return found


def clean_result(ctx, identity, entries, out_dir, targets, configs, arch, execute, target_source):
    total = None if ctx.parsed.get("no_size") else sum(e.size_kib or 0 for e in entries if e.size_kib is not None)
    data = {"mode": "execute" if execute else "preview", "targets": targets, "target_source": target_source,
            "configurations": configs,
            "arch": arch, "out_dir": str(out_dir) if out_dir else None, "total_kib": total,
            "entries": [{"name": e.name, "path": str(e.path), "size_kib": e.size_kib, "outcome": e.outcome,
                         "detail": e.detail} for e in entries]}
    lines = ["%s in %s" % ("Deleting" if execute else "Preview (nothing is deleted)",
                           out_dir or identity.src / "out")]
    source_label = {"host": "host default only", "configured": "configured default only",
                    "explicit": "explicit selection"}[target_source]
    lines.append("Platforms: %s (%s)" % (", ".join(targets), source_label))
    lines.append("Configurations: %s; architecture: %s" % (", ".join(configs), arch or "all"))
    if target_source != "explicit":
        lines.append("Other platforms are not checked. To preview every platform, run 'bcore clean all'.")
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
    positionals=(Positional("target", help="mac, android, ios, or all; omitted means the default target only."),),
    options=(Opt("--configuration", "configuration", choices=("debug", "release", "all"),
                 help="Configuration to match (default: all).", metavar="CONFIG"),
             Opt("--arch", "arch", help="Only this architecture, such as arm64. Core names x64 outputs without a suffix.", metavar="ARCH"),
             Opt("--execute", "execute", takes_value=False, help="Delete the listed directories."),
             Opt("--no-size", "no_size", takes_value=False, help="Skip size calculation.")),
    side_effects="Preview writes nothing. --execute deletes matching directories directly under the "
                 "selected checkout's src/out. Stop builds first; one operator per checkout.",
    examples=("bcore clean", "bcore clean android --configuration debug --arch arm64", "bcore clean all --execute"))
