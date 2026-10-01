# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The repositories a source sync can reset, and the local work they hold.

Core's sync runs gclient with `--reset` for Chromium and again for Core, and gclient resets every Git
dependency it manages. Each gclient records those dependencies in an entries file next to its
configuration (`.gclient_entries` in the workspace, `.brave_gclient_entries` in Core); entries whose name
contains a colon are packages, not repositories.
"""

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
    try:
        return json.loads(baseline_path(identity, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_baseline(identity, current, root=None):
    atomic_write(baseline_path(identity, root), json.dumps(current, sort_keys=True, indent=1) + "\n")


def checkpoint(identity, root=None, log=None):
    """Remember the tracked changes present after a sync that started with none of your work in the way.

    Everything present then was already accounted for or was made by the sync and the steps it runs, so it is
    Core's output. A later difference from this record is a change made since.
    """
    write_baseline(identity, snapshot(identity, sync_repositories(identity), log), root)


def local_work(identity, scope, expected, baseline, root=None, log=None, adopt=False):
    """Local work in the scope that a sync would reset, as conflict records.

    Core is checked for any change, untracked files included. Every other repository is checked for tracked
    files that differ from HEAD, except paths that still hold what a scaffold step wrote (`expected`) or what
    Core's tools had left when the last sync finished (`baseline`). Incomplete discovery is itself a conflict.
    Staged changes are checked separately and cannot be excused by working-file checksums.
    With `adopt` the tracked changes present now are recorded as Core's output instead of being reported;
    that never hides incomplete discovery or changes in Core.
    """
    conflicts = [{"path": problem, "reason": "incomplete evidence"} for problem in scope.problems]
    current = {}
    staged = set()
    for repository in scope.repositories:
        changes = gitstate.inspect_changes(repository, log)
        if repository == identity.core:
            if changes.all:
                conflicts.append({"path": str(identity.core),
                                  "reason": "Core has %d uncommitted change(s)" % len(changes.all)})
            continue
        staged |= {repository / relative for relative in changes.staged}
        files = {relative: sha256_or_none(repository / relative) for relative in changes.tracked}
        if files:
            current[_label(identity, repository)] = files
        conflicts += [{"path": _label(identity, repository / relative),
                       "reason": "untracked work could collide with incoming sync files; incoming paths are unknown"}
                      for relative in sorted(changes.untracked)]
    conflicts += [{"path": _label(identity, path), "reason": "staged changes would be discarded by sync"}
                  for path in sorted(staged)]
    if adopt and not conflicts:
        write_baseline(identity, current, root)
        baseline = current
    for label, files in current.items():
        base = identity.src if label == "." else identity.src / label
        for relative, digest in sorted(files.items()):
            if base / relative in staged:
                continue
            if base / relative in expected or (relative in baseline.get(label, {}) and baseline[label][relative] == digest):
                continue
            conflicts.append({"path": _label(identity, base / relative),
                              "reason": "tracked file with local changes in %s; a sync resets it" % (
                                  "Chromium" if label == "." else label)})
    return conflicts
