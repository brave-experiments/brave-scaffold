# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch metadata inspection (drift) and safe patch preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..common.config import atomic_write
from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair
from . import gitstate
from .patch_inventory import PatchRepository, read_inventory, sha256_file, sha256_or_none
from .records import checkout_key, store_root

SOURCE_CHANGED = "source changed after patch applied"
SOURCE_MISSING = "source file missing"
PATCH_CHANGED = "patch file changed"
PATCH_REMOVED = "patch file removed"


@dataclass
class DriftedFile:
    repo_path: str
    reasons: set = field(default_factory=set)
    patchinfo_paths: set = field(default_factory=set)
    patch_paths: set = field(default_factory=set)
    numstat: str = "not available"
    repository: Path | None = None  # the repository that owns the file
    relative: str | None = None  # the file's path inside that repository

    def to_dict(self):
        return {"path": self.repo_path, "reasons": sorted(self.reasons), "numstat": self.numstat,
                "patchinfo": sorted(str(p) for p in self.patchinfo_paths),
                "patches": sorted(str(p) for p in self.patch_paths)}


@dataclass
class DriftReport:
    files: dict = field(default_factory=dict)
    incomplete: list = field(default_factory=list)
    unverifiable: list = field(default_factory=list)
    patchinfo_count: int = 0
    patched_paths: dict = field(default_factory=dict)  # source-relative path -> recorded checksum
    inventory: object = None

    @property
    def complete(self):
        return not self.incomplete


def _add(report, repo_path, reason, patchinfo, patch, repository=None, relative=None):
    entry = report.files.setdefault(repo_path, DriftedFile(repo_path, repository=repository, relative=relative))
    entry.reasons.add(reason)
    entry.patchinfo_paths.add(patchinfo)
    entry.patch_paths.add(patch)


def _incomplete(report, message):
    report.incomplete.append(message)
    report.unverifiable.append(message)


def collect_drift(identity, inventory=None):
    """Compare materialized files with the patch metadata of every patched repository. Read-only.

    Missing or malformed metadata is reported as incomplete evidence, never as a clean result.
    """
    report = DriftReport()
    if not (identity.core / "patches").is_dir():
        _incomplete(report, "patches directory is missing: %s" % (identity.core / "patches"))
        return report
    inventory = inventory or read_inventory(identity)
    report.inventory = inventory
    for problem in inventory.problems:
        _incomplete(report, problem)
    informed = [entry for entry in inventory.entries if entry.has_info]
    report.patchinfo_count = len(informed)
    if not informed:
        _incomplete(report, "no .patchinfo metadata files were found")
    for entry in informed:
        for problem in entry.metadata_problems:
            _incomplete(report, problem)
        if entry.metadata_problems:
            continue
        removed = not entry.has_patch
        changed = not removed and sha256_or_none(entry.patch) != entry.patch_checksum
        for relative, checksum in entry.recorded.items():
            key = entry.repository.source_path(relative)
            report.patched_paths[key] = checksum
            current = sha256_or_none(identity.src / key)
            where = (entry.repository.path, relative)
            if current is None:
                _add(report, key, SOURCE_MISSING, entry.info, entry.patch, *where)
            elif current != checksum:
                _add(report, key, SOURCE_CHANGED, entry.info, entry.patch, *where)
            if removed:
                _add(report, key, PATCH_REMOVED, entry.info, entry.patch, *where)
            elif changed:
                _add(report, key, PATCH_CHANGED, entry.info, entry.patch, *where)
    unrecorded = [entry.patch.name for entry in inventory.entries if entry.has_patch and not entry.has_info]
    if unrecorded:
        report.incomplete.append("%d patch file(s) have no metadata yet (for example %s)" % (
            len(unrecorded), unrecorded[0]))
    return report


def add_numstats(identity, report, log=None):
    for entry in report.files.values():
        repository = entry.repository or identity.src
        result = run_capture(["git", "-C", str(repository), "diff", "--numstat", "--", entry.relative or entry.repo_path],
                             str(repository), None, log, timeout=60)
        entry.numstat = result.stdout.strip() or "0\t0\t(no git diff)" if result.returncode == 0 else "not available"


# --- receipts and preparation ---------------------------------------------------------


def receipt_path(identity, root=None):
    return store_root(root) / "state" / checkout_key(identity.core) / "patch-receipt.json"


def read_receipt(identity, root=None):
    try:
        return json.loads(receipt_path(identity, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _git(identity, repo, args, log):
    return run_capture(["git", "-C", str(repo), *args], str(repo), None, log, timeout=120)


def patch_inputs(identity, log=None):
    """Tree ids of the patch inputs at HEAD and whether the working copies differ."""
    trees = {}
    for name in ("patches", "rewrite"):
        result = _git(identity, identity.core, ["rev-parse", "--verify", "HEAD:" + name], log)
        trees[name] = result.stdout.strip() if result.returncode == 0 else None
    status = _git(identity, identity.core, ["status", "--porcelain", "--untracked-files=all", "--", "patches",
                                            "rewrite"], log)
    dirty = status.stdout.splitlines() if status.returncode == 0 else None
    return trees, dirty


def snapshot_files(identity, report):
    """Current checksums of every patched Chromium file that exists."""
    snapshot = {}
    for repo_path in report.patched_paths:
        try:
            snapshot[repo_path] = sha256_file(identity.src / repo_path)
        except OSError:
            continue
    return snapshot


_CARRY = object()


def write_receipt(identity, trees, files, root=None, extra_expected=_CARRY):
    """Record patch inputs and file checksums. Expected extra changes are kept unless replaced."""
    if extra_expected is _CARRY:
        extra_expected = (read_receipt(identity, root) or {}).get("extra_expected") or {}
    data = {"patches_tree": trees.get("patches"), "rewrite_tree": trees.get("rewrite"), "files": files,
            "extra_expected": extra_expected}
    atomic_write(receipt_path(identity, root), json.dumps(data, sort_keys=True, indent=1) + "\n")


CORE_WRITTEN = ("chrome/VERSION", "chrome/VERSION.chromium")  # Core's version update writes both files


def core_output(identity):
    """Content of the files Core's patch step writes besides patch targets, keyed by source-relative path."""
    return {path: digest for path in CORE_WRITTEN if (digest := sha256_or_none(identity.src / path))}


def core_written_paths(identity, root=None):
    """Paths that still hold what Core's patch step or a verified preparation step last wrote there."""
    extra = (read_receipt(identity, root) or {}).get("extra_expected") or {}
    return {identity.src / path for path, digest in extra.items() if digest and sha256_or_none(identity.src / path) == digest}


def record_extra_expected(identity, root=None):
    """After a verified step changed patched files, remember exactly what it wrote there."""
    receipt = read_receipt(identity, root)
    if receipt is None:
        return
    report = collect_drift(identity)
    receipt["extra_expected"] = {**core_output(identity),
                                 **{repo_path: sha256_or_none(identity.src / repo_path)
                                    for repo_path, entry in report.files.items() if entry.reasons == {SOURCE_CHANGED}}}
    atomic_write(receipt_path(identity, root), json.dumps(receipt, sort_keys=True, indent=1) + "\n")


@dataclass
class PatchPlan:
    action: str  # "current" | "apply" | "conflict"
    reason: str
    report: DriftReport
    trees: dict
    conflicts: list = field(default_factory=list)
    receipt_used: bool = False
    writes: list = field(default_factory=list)  # every source-relative path applying the stale patches can write
    metadata_writes: list = field(default_factory=list)  # absolute paths of the metadata files Core rewrites


def plan_patch_preparation(identity, log=None, root=None, extra_expected=None):
    """Decide whether patches need applying and whether applying could lose local work."""
    inventory = read_inventory(identity)
    report = collect_drift(identity, inventory)
    receipt = read_receipt(identity, root)
    # Another verified step (for example Android support preparation) may change patched files on
    # top of Core's patches. Those files are expected while they still hold what that step wrote.
    extra = {**((receipt or {}).get("extra_expected") or {}), **(extra_expected or {})}
    for repo_path, expected in extra.items():
        entry = report.files.get(repo_path)
        if entry is not None and entry.reasons == {SOURCE_CHANGED} and sha256_or_none(identity.src / repo_path) == expected:
            del report.files[repo_path]
    trees, dirty = patch_inputs(identity, log)
    inputs_changed = (receipt is None or receipt.get("patches_tree") != trees["patches"]
                      or receipt.get("rewrite_tree") != trees["rewrite"])
    if report.unverifiable:
        return PatchPlan("conflict", "Local Chromium edits cannot be told apart from patch results because the "
                         "patch metadata is unusable.", report, trees,
                         [{"path": reason, "reason": "metadata"} for reason in report.unverifiable[:5]])
    if not report.files and report.complete and not dirty:
        return PatchPlan("current", "Materialized files match the patch metadata.", report, trees)
    known = (receipt or {}).get("files", {})
    stale = [entry for entry in inventory.entries if entry.stale_reason()]
    conflicts, writes = write_set_conflicts(identity, stale, known, extra, log)
    if conflicts:
        return PatchPlan("conflict", "Applying patches could overwrite local Chromium edits.", report, trees,
                         conflicts, receipt is not None, writes)
    reasons = []
    if report.files:
        reasons.append("%d patched file(s) differ from the metadata" % len(report.files))
    if dirty:
        reasons.append("patch or rewrite inputs have local changes")
    if inputs_changed and not reasons:
        reasons.append("patch inputs changed or are unrecorded")
    if report.incomplete:
        reasons.append("metadata incomplete: " + "; ".join(report.incomplete[:2]))
    return PatchPlan("apply", "; ".join(reasons) or "patch state unverified", report, trees, [],
                     receipt is not None, writes,
                     sorted(str(entry.info) for entry in stale if entry.has_patch))


def write_set_conflicts(identity, stale, known, extra, log):
    """(conflicts, writes) for applying the stale patches.

    Applying a patch resets every file it targets now or recorded earlier, so all of them are checked, not only
    the ones the old metadata lists. A file is safe when it holds content the scaffold or the metadata
    recorded, or when nothing claims it and Git sees no local work there. Anything else could be lost: staged,
    unstaged, deleted, renamed, and untracked work all count, and so does any Git failure or unreadable patch.
    """
    conflicts, keys = [], {}
    for entry in stale:
        if entry.has_patch and entry.target_problem:
            conflicts.append({"path": str(entry.patch.relative_to(identity.core)), "reason": entry.target_problem})
        for relative in entry.write_set():
            keys.setdefault(entry.repository.source_path(relative), []).append((entry, relative))
    by_repository = {}
    for key, owners in keys.items():
        entry, relative = owners[0]
        by_repository.setdefault(entry.repository, {})[relative] = key
    chromium = PatchRepository("", identity.src, identity.core / "patches")
    for relative in CORE_WRITTEN:
        keys.setdefault(relative, [])
        by_repository.setdefault(chromium, {})[relative] = relative
    for repository, relatives in by_repository.items():
        try:
            if repository.path.exists() and not (repository.path / ".git").exists():
                raise ScaffoldError("PREPARATION_CONFLICT", "%s is not a Git repository, so local work in it cannot "
                                    "be inspected." % repository.path)
            changes = gitstate.inspect_changes(repository.path, log, sorted(relatives)) \
                if repository.path.exists() else gitstate.Changes()
            dirty, staged = changes.all, changes.staged
            if repository == chromium:
                tracked = gitstate.tracked_paths(repository.path, CORE_WRITTEN, log)
                dirty |= {path for path in CORE_WRITTEN if path not in tracked
                          and ((identity.src / path).exists() or (identity.src / path).is_symlink())}
        except ScaffoldError as error:
            conflicts.append({"path": repository.rel or ".", "reason": error.message})
            continue
        for relative, key in sorted(relatives.items(), key=lambda item: item[1]):
            if relative in staged:
                conflicts.append({"path": key, "reason": "has staged changes that patch preparation could discard"})
                continue
            reason = _write_conflict(identity, key, relative, keys[key], relative in dirty, known, extra)
            if reason:
                conflicts.append({"path": key, "reason": reason})
    return conflicts, sorted(keys)


def _write_conflict(identity, key, relative, owners, dirty, known, extra):
    if (identity.src / key).is_symlink():
        return "local symlink would redirect a patch or version write; save or remove the link first"
    # Clean tracked bytes are safe to replace even when old patch metadata names different output.
    # Staged changes were rejected by the caller; symlinks remain protected above.
    if not dirty:
        return None
    recorded = {entry.recorded[relative] for entry, _ in owners if relative in entry.recorded}
    accepted = recorded | {value for value in (known.get(key), extra.get(key)) if value}
    current = sha256_or_none(identity.src / key)
    if current is not None and current in accepted:
        return None
    if not recorded and key not in known and key not in extra:
        return ("has local changes (staged, unstaged, deleted, renamed, or untracked) and a patch will be "
                "applied to it") if dirty else None
    if key not in known:
        return "no earlier record of this file; cannot tell local edits from results of an older patch"
    if current is None:
        return "file is missing"
    return "changed since patches were last applied here"


def conflict_error(plan, identity):
    return ScaffoldError(
        "PREPARATION_CONFLICT",
        "%s %d file(s) need your review before patches can be applied." % (plan.reason, len(plan.conflicts)),
        details={"files": plan.conflicts[:50], "total": len(plan.conflicts), "checkout": str(identity.core)},
        repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)],
                        note="Review the differences. Save wanted edits with 'bdev patches update' or restore the "
                             "files, then run 'bpm run apply_patches' yourself if you want stale files replaced.")])
