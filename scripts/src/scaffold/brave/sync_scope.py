# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Dependency repository discovery and tracked-change snapshots for build and support checks."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from . import gitstate
from .patch_inventory import sha256_or_none

WORKSPACE_ENTRIES = ".gclient_entries"
CORE_ENTRIES = ".brave_gclient_entries"


@dataclass
class SyncScope:
    repositories: list  # existing Git repositories gclient records, Chromium and Core first
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
