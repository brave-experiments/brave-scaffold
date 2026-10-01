# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Repository discovery and local-work evidence for the selected sync operation."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..common.config import atomic_write
from . import gitstate
from .patch_inventory import sha256_or_none
from .records import checkout_key, store_root

WORKSPACE_ENTRIES = ".gclient_entries"
CORE_ENTRIES = ".brave_gclient_entries"


@dataclass
class SyncScope:
    repositories: list  # existing Git repositories a sync can reset, Chromium and Core first
    problems: list = field(default_factory=list)  # reasons the list cannot be trusted to be complete

    @property
    def complete(self):
        return not self.problems


def read_entries(path):
    """The dependency names gclient recorded in an entries file, or None when it cannot be read."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "entries" for t in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, dict) and all(isinstance(name, str) for name in value):
                    return list(value)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        pass
    return None


def _repositories_in(base, names, problems, label):
    found = []
    if names is None:
        problems.append("%s is missing or unreadable, so the repositories gclient manages are unknown" % label)
        return found
    for name in names:
        if ":" in name:
            continue
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts:
            problems.append("%s names %r outside the checkout" % (label, name))
            continue
        path = base / name
        if (path / ".git").exists():
            found.append(path)
    return found


def sync_repositories(identity):
    """Chromium, Core, and every dependency repository gclient records that exists on disk."""
    problems, found = [], [identity.src, identity.core]
    found += _repositories_in(identity.workspace, read_entries(identity.workspace / WORKSPACE_ENTRIES), problems,
                              WORKSPACE_ENTRIES)
    found += _repositories_in(identity.core, read_entries(identity.core / CORE_ENTRIES), problems, CORE_ENTRIES)
    unique = list(dict.fromkeys(found))
    return SyncScope(unique, problems)


def _label(identity, path):
    return "." if path == identity.src else str(path.relative_to(identity.src)) if path.is_relative_to(identity.src) \
        else str(path)


def snapshot(identity, scope, log=None, include_core=False):
    """{repository label: {path: checksum}} of the tracked changes in every repository (Core only on request).

    A deleted file has a checksum of None. Raises when Git cannot inspect a repository completely.
    """
    found = {}
    for repository in scope.repositories:
        if repository == identity.core and not include_core:
            continue
        files = {relative: sha256_or_none(repository / relative)
                 for relative in gitstate.tracked_changes(repository, log)}
        if files:
            found[_label(identity, repository)] = files
    return found


def changed_between(identity, before, after):
    """Absolute paths whose tracked-change state differs between two snapshots."""
    changed = set()
    for label in before.keys() | after.keys():
        base = identity.src if label == "." else identity.src / label
        old, new = before.get(label, {}), after.get(label, {})
        changed |= {base / relative for relative in old.keys() | new.keys()
                    if old.get(relative, "clean") != new.get(relative, "clean")}
    return changed


def baseline_path(identity, root=None):
    return store_root(root) / "state" / checkout_key(identity.core) / "sync-baseline.json"


def read_baseline(identity, root=None):
    """Only identified successful-sync output can excuse local working bytes."""
    try:
        data = json.loads(baseline_path(identity, root).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema_version") == 1 \
                and data.get("origin") == "successful-sync" and data.get("core") == str(identity.core) \
                and isinstance(data.get("files"), dict):
            return data["files"]
    except (OSError, ValueError):
        pass
    return {}


def checkpoint(identity, root=None, log=None, before=None):
    """Remember the tracked changes left by a successful guarded sync.

    Legacy baselines lack origin evidence and remain on disk, but cannot excuse
    local work: they may have been saved by rejected or failed blanket adoption.
    """
    current = snapshot(identity, sync_repositories(identity), log, include_core=True)
    previous = read_baseline(identity, root)
    files = {}
    for label, paths in current.items():
        accepted = {path: digest for path, digest in paths.items()
                    if (before is not None and before.get(label, {}).get(path, "clean") != digest)
                    or (path in previous.get(label, {}) and previous[label][path] == digest)}
        if accepted:
            files[label] = accepted
    data = {"schema_version": 1, "origin": "successful-sync", "core": str(identity.core), "files": files}
    atomic_write(baseline_path(identity, root), json.dumps(data, sort_keys=True, indent=1) + "\n")


def overlaps(path, writes):
    """A write can replace a path, its directory, or a file obstructing its parents."""
    return any(path == write or path.is_relative_to(write) or write.is_relative_to(path) for write in writes)


def local_work(identity, scope, expected, baseline, log=None, *, reset_repositories=None, writes=(), unknown_writes=(), reset_upstream=True, incoming_trees=None):
    """Protect only work threatened by reset, patch, hooks, or unknown mutation scope.

    A non-reset gclient update uses Git's clean checks and non-forced checkout/rebase;
    it can refuse local work but does not discard it. Unmanaged Core is never reset.
    Generated worktree bytes never excuse a staged change in a reset repository.
    """
    incoming_trees = incoming_trees or {}
    resets = set(scope.repositories) if reset_repositories is None else set(reset_repositories)
    conflicts = [{"path": problem, "reason": "incomplete evidence"} for problem in scope.problems]
    for repository in scope.repositories:
        changes = gitstate.inspect_changes(repository, log)
        if reset_upstream and repository in resets:
            ahead = gitstate._git(repository, ["rev-list", "--count", "@{upstream}..HEAD"], log)
            if ahead.returncode == 0 and (ahead.truncated or ahead.stdout.strip() != "0"):
                conflicts.append({"path": _label(identity, repository / ".git/HEAD"),
                                  "reason": "local commits would be discarded by gclient reset --upstream; "
                                            "retain them on a branch or select a non-reset sync"})
        label = _label(identity, repository)
        collision_trees = None
        for relative in sorted(changes.all):
            path = repository / relative
            known = not path.is_symlink() and (path in expected or (relative in baseline.get(label, {})
                                             and baseline[label][relative] == sha256_or_none(path)))
            # Known working output cannot excuse staged bytes, but otherwise
            # needs no comparison against thousands of patch/hook write paths.
            if known and relative not in changes.staged:
                continue
            threatened = repository in resets
            output = overlaps(path, writes)
            if not threatened and not output and not unknown_writes:
                continue
            if relative in changes.staged and (threatened or unknown_writes):
                reason = "staged changes would be discarded by gclient reset" if threatened else \
                    "staged work could be discarded by " + "; ".join(unknown_writes)
            elif output:
                # A hook writes the working file, even when gclient skips this repository.
                # Staged work does not make different wanted working bytes disposable.
                if known and relative not in changes.staged:
                    continue
                operations = sorted({operation for write, operation in writes.items() if overlaps(path, [write])}) \
                    if isinstance(writes, dict) else ["patch or hook output"]
                reason = "; ".join(operations) + " could overwrite local work; save wanted files first; --nohooks helps only if a regular hook is the sole writer"
            elif unknown_writes and not known:
                # Unknown hooks cannot establish preservation of developer working bytes.
                reason = "; ".join(unknown_writes) + "; inspect the write scope; --nohooks does not skip pre-DEPS hooks or other writes"
            elif relative in changes.untracked:
                if known:
                    continue
                if not unknown_writes and repository in incoming_trees:
                    if collision_trees is None:
                        collision_trees = reset_paths(repository, incoming_trees[repository], log)
                    if collision_trees is not None and not path_collision(relative, collision_trees):
                        continue
                reason = "untracked work could collide with gclient reset; incoming paths are unknown or overlap"
            elif known:
                continue
            else:
                reason = "tracked local changes would be discarded by gclient reset; save wanted edits first"
            conflicts.append({"path": _label(identity, path), "reason": reason})
    return conflicts


def reset_paths(repository, incoming, log=None):
    """Read both trees touched by a reviewed detached-HEAD reset and checkout."""
    branch = gitstate._git(repository, ['symbolic-ref', '--quiet', 'HEAD'], log)
    if branch.returncode != 1:
        return None
    reader, revision = incoming
    paths = set()
    for repo, ref in ((repository, 'HEAD'), (reader, revision)):
        result = gitstate._git(repo, ['ls-tree', '-rz', '--name-only', ref], log)
        if result.returncode or result.truncated or '\ufffd' in result.stdout:
            return None
        paths.update(path for path in result.stdout.split('\0') if path)
    return paths


def path_collision(relative, tracked_paths):
    # Be conservative on case-insensitive filesystems, including macOS.
    relative = relative.casefold()
    return any(relative == tracked or relative.startswith(tracked + '/') or tracked.startswith(relative + '/')
               for tracked in map(str.casefold, tracked_paths))
