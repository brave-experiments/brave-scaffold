# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch metadata inspection (drift) and safe patch preparation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..common.config import atomic_write
from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair
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
    patched_paths: dict = field(default_factory=dict)

    @property
    def complete(self):
        return not self.incomplete


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add(report, repo_path, reason, patchinfo, patch):
    entry = report.files.setdefault(repo_path, DriftedFile(repo_path))
    entry.reasons.add(reason)
    entry.patchinfo_paths.add(patchinfo)
    entry.patch_paths.add(patch)


def collect_drift(identity):
    """Compare materialized Chromium files with the patch metadata. Read-only.

    Missing or malformed metadata is reported as incomplete evidence, never as a
    clean result.
    """
    report = DriftReport()
    patches = identity.core / "patches"
    if not patches.is_dir():
        report.incomplete.append("patches directory is missing: %s" % patches)
        report.unverifiable.append(report.incomplete[-1])
        return report
    patchinfo_files = sorted(patches.glob("*.patchinfo"))
    report.patchinfo_count = len(patchinfo_files)
    if not patchinfo_files:
        report.incomplete.append("no .patchinfo metadata files were found")
        report.unverifiable.append(report.incomplete[-1])
    described = set()
    for info_path in patchinfo_files:
        patch_path = info_path.with_suffix(".patch")
        described.add(patch_path.name)
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            report.incomplete.append("cannot read %s: %s" % (info_path.name, error))
            report.unverifiable.append(report.incomplete[-1])
            continue
        applies = info.get("appliesTo") if isinstance(info, dict) else None
        if not isinstance(applies, list):
            report.incomplete.append("malformed appliesTo in %s" % info_path.name)
            report.unverifiable.append(report.incomplete[-1])
            continue
        missing = not patch_path.exists()
        changed = False
        expected = info.get("patchChecksum")
        if not missing and isinstance(expected, str):
            try:
                changed = sha256_file(patch_path) != expected
            except OSError:
                changed = True
        for entry in applies:
            repo_path = entry.get("path") if isinstance(entry, dict) else None
            checksum = entry.get("checksum") if isinstance(entry, dict) else None
            if not isinstance(repo_path, str) or not isinstance(checksum, str):
                report.incomplete.append("incomplete appliesTo entry in %s" % info_path.name)
                continue
            report.patched_paths[repo_path] = checksum
            source = identity.src / repo_path
            if not source.exists():
                _add(report, repo_path, SOURCE_MISSING, info_path, patch_path)
            else:
                try:
                    if sha256_file(source) != checksum:
                        _add(report, repo_path, SOURCE_CHANGED, info_path, patch_path)
                except OSError:
                    _add(report, repo_path, SOURCE_CHANGED, info_path, patch_path)
            if missing:
                _add(report, repo_path, PATCH_REMOVED, info_path, patch_path)
            elif changed:
                _add(report, repo_path, PATCH_CHANGED, info_path, patch_path)
    unrecorded = sorted(p.name for p in patches.glob("*.patch") if p.name not in described)
    if unrecorded:
        report.incomplete.append("%d patch file(s) have no metadata yet (for example %s)" % (
            len(unrecorded), unrecorded[0]))
    return report


def add_numstats(identity, report, log=None):
    for entry in report.files.values():
        result = run_capture(["git", "-C", str(identity.src), "diff", "--numstat", "--", entry.repo_path],
                             str(identity.src), None, log, timeout=60)
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


def _sha_or_none(path):
    try:
        return sha256_file(path)
    except OSError:
        return None


_CARRY = object()


def write_receipt(identity, trees, files, root=None, extra_expected=_CARRY):
    """Record patch inputs and file checksums. Expected extra changes are kept unless replaced."""
    if extra_expected is _CARRY:
        extra_expected = (read_receipt(identity, root) or {}).get("extra_expected") or {}
    data = {"patches_tree": trees.get("patches"), "rewrite_tree": trees.get("rewrite"), "files": files,
            "extra_expected": extra_expected}
    atomic_write(receipt_path(identity, root), json.dumps(data, sort_keys=True, indent=1) + "\n")


def record_extra_expected(identity, root=None):
    """After a verified step changed patched files, remember exactly what it wrote there."""
    receipt = read_receipt(identity, root)
    if receipt is None:
        return
    report = collect_drift(identity)
    receipt["extra_expected"] = {repo_path: _sha_or_none(identity.src / repo_path)
                                 for repo_path, entry in report.files.items() if entry.reasons == {SOURCE_CHANGED}}
    atomic_write(receipt_path(identity, root), json.dumps(receipt, sort_keys=True, indent=1) + "\n")


@dataclass
class PatchPlan:
    action: str  # "current" | "apply" | "conflict"
    reason: str
    report: DriftReport
    trees: dict
    conflicts: list = field(default_factory=list)
    receipt_used: bool = False


def plan_patch_preparation(identity, log=None, root=None):
    """Decide whether patches need applying and whether applying could lose local work."""
    report = collect_drift(identity)
    receipt = read_receipt(identity, root)
    # Another verified step (for example Android support preparation) may change patched files on
    # top of Core's patches. Those files are expected while they still hold what that step wrote.
    for repo_path, expected in ((receipt or {}).get("extra_expected") or {}).items():
        entry = report.files.get(repo_path)
        if entry is not None and entry.reasons == {SOURCE_CHANGED} and _sha_or_none(identity.src / repo_path) == expected:
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
    conflicts = []
    for repo_path, entry in sorted(report.files.items()):
        if repo_path not in known:
            conflicts.append({"path": repo_path, "reason": "no earlier record of this file; cannot tell "
                              "local edits from results of an older patch"})
            continue
        if SOURCE_MISSING in entry.reasons:
            conflicts.append({"path": repo_path, "reason": "file is missing"})
            continue
        try:
            current = sha256_file(identity.src / repo_path)
        except OSError:
            conflicts.append({"path": repo_path, "reason": "file cannot be read"})
            continue
        if current != known[repo_path]:
            conflicts.append({"path": repo_path, "reason": "changed since patches were last applied here"})
    unrecorded = _unrecorded_patch_targets(identity, report, known, log)
    conflicts.extend(unrecorded)
    if conflicts:
        return PatchPlan("conflict", "Applying patches could overwrite local Chromium edits.", report, trees,
                         conflicts, receipt is not None)
    reasons = []
    if report.files:
        reasons.append("%d patched file(s) differ from the metadata" % len(report.files))
    if dirty:
        reasons.append("patch or rewrite inputs have local changes")
    if inputs_changed and not reasons:
        reasons.append("patch inputs changed or are unrecorded")
    if report.incomplete:
        reasons.append("metadata incomplete: " + "; ".join(report.incomplete[:2]))
    return PatchPlan("apply", "; ".join(reasons) or "patch state unverified", report, trees, [], receipt is not None)


_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)


def _unrecorded_patch_targets(identity, report, known, log):
    """Targets of patches that have no metadata yet, when they hold uncommitted Chromium edits."""
    patches = identity.core / "patches"
    if not patches.is_dir():
        return []
    conflicts = []
    described = {p.name for p in patches.glob("*.patchinfo")}
    for patch in sorted(patches.glob("*.patch")):
        if patch.with_suffix(".patchinfo").name in described:
            continue
        try:
            targets = _DIFF_HEADER.findall(patch.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for target in targets:
            if target in known or not (identity.src / target).exists():
                continue
            diff = _git(identity, identity.src, ["diff", "--quiet", "--", target], log)
            if diff.returncode == 1:
                conflicts.append({"path": target, "reason": "has uncommitted edits and a patch without metadata "
                                  "will be applied to it"})
    return conflicts


def conflict_error(plan, identity):
    return ScaffoldError(
        "PREPARATION_CONFLICT",
        "%s %d file(s) need your review before patches can be applied." % (plan.reason, len(plan.conflicts)),
        details={"files": plan.conflicts[:50], "total": len(plan.conflicts), "checkout": str(identity.core)},
        repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)],
                        note="Review the differences. Save wanted edits with 'bdev patches update' or restore the "
                             "files, then run 'bpm run apply_patches' yourself if you want stale files replaced.")])
