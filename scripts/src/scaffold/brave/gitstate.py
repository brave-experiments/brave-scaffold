# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only Git questions about specific paths, shared by every guard that protects local work."""

from __future__ import annotations

from pathlib import Path

from ..common.procs import run_capture
from ..common.results import ScaffoldError


def _git(repo, args, log, timeout=120):
    return run_capture(["git", "--literal-pathspecs", "-C", str(repo), *args], str(repo), None, log, timeout=timeout)


def changed_paths(repo, paths, log=None):
    """The given repository-relative paths that differ from HEAD in the index or worktree, or are untracked.

    Renames report both names. Raises when Git cannot answer: an unknown state is never treated as clean.
    """
    paths = list(paths)
    if not paths:
        return set()
    result = _git(repo, ["status", "--porcelain", "-z", "--untracked-files=all", "--", *paths], log)
    if result.returncode != 0:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Local changes in %s could not be inspected (git status exit %d), so nothing was "
                            "changed." % (repo, result.returncode), details={"repository": str(repo)})
    changed, entries = set(), iter(result.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        changed.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            changed.add(next(entries, ""))
    return changed


def tracked_paths(repo, paths, log=None):
    """The given repository-relative paths that this repository tracks."""
    paths = list(paths)
    if not paths:
        return set()
    result = _git(repo, ["ls-files", "-z", "--", *paths], log)
    if result.returncode != 0:
        raise ScaffoldError("PREPARATION_CONFLICT", "Tracked files in %s could not be listed." % repo,
                            details={"repository": str(repo)})
    return {name for name in result.stdout.split("\0") if name}


def nested_repositories(root):
    """The root and its immediate child directories that are Git repositories."""
    root = Path(root)
    children = sorted(child for child in root.iterdir() if child.is_dir() and not child.is_symlink()
                      and (child / ".git").exists())
    return [root, *children]
