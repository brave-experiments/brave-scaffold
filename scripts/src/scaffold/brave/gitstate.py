# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only Git questions about specific paths, shared by every guard that protects local work."""

from __future__ import annotations

import os
from pathlib import Path

from ..common.procs import run_capture
from ..common.results import ScaffoldError


EVIDENCE_BYTES = 64 << 20


def _git(repo, args, log, timeout=120):
    return run_capture(["git", "--literal-pathspecs", "-C", str(repo), *args], str(repo), None, log, timeout=timeout,
                       max_bytes=EVIDENCE_BYTES)


def _require_root(repo, log):
    """Stop unless Git treats `repo` itself as a repository, not an enclosing one.

    A damaged repository directory makes Git fall through to the repository around it, whose answer would
    describe the wrong files.
    """
    top = _git(repo, ["rev-parse", "--show-toplevel"], log, timeout=30)
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(repo):
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "%s is not readable as a Git repository, so its local changes could not be inspected; "
                            "nothing was changed." % repo, details={"repository": str(repo)})


def _status(repo, pathspec, untracked, log):
    _require_root(repo, log)
    result = _git(repo, ["status", "--porcelain", "-z", "--untracked-files=" + untracked, *pathspec], log, timeout=600)
    if result.returncode != 0 or result.truncated:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Local changes in %s could not be inspected (%s), so nothing was changed." % (
                                repo, "git status output was too large to read completely" if result.truncated
                                else "git status exit %d" % result.returncode), details={"repository": str(repo)})
    changed, entries = set(), iter(result.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        changed.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            changed.add(next(entries, ""))
    return changed


def changed_paths(repo, paths, log=None):
    """The given repository-relative paths that differ from HEAD in the index or worktree, or are untracked.

    Renames report both names. Raises when Git cannot answer: an unknown state is never treated as clean.
    """
    paths = list(paths)
    return _status(repo, ["--", *paths], "all", log) if paths else set()


def local_changes(repo, log=None):
    """Every path with local work in the repository: tracked changes and untracked files."""
    return _status(repo, [], "all", log)


def tracked_changes(repo, log=None):
    """Every tracked file of the repository whose index or worktree content differs from HEAD.

    Untracked files are not listed. Raises when Git cannot answer completely.
    """
    return _status(repo, [], "no", log)


def tracked_paths(repo, paths, log=None):
    """The given repository-relative paths that this repository tracks."""
    paths = list(paths)
    if not paths:
        return set()
    result = _git(repo, ["ls-files", "-z", "--", *paths], log)
    if result.returncode != 0 or result.truncated:
        raise ScaffoldError("PREPARATION_CONFLICT", "Tracked files in %s could not be listed completely." % repo,
                            details={"repository": str(repo)})
    return {name for name in result.stdout.split("\0") if name}


def head_commit(repo, log=None):
    """The commit HEAD names, asked of Git so every layout (linked, separate, packed) gives the same answer."""
    result = _git(repo, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], log, timeout=30)
    text = result.stdout.strip()
    return text if result.returncode == 0 and not result.truncated and text else None


def head_description(repo, log=None):
    """{"branch": name} or {"detached": commit} for HEAD, or None when it cannot be read."""
    branch = _git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], log, timeout=30)
    if branch.returncode == 0 and branch.stdout.strip():
        return {"branch": branch.stdout.strip()}
    commit = head_commit(repo, log)
    return {"detached": commit} if commit else None


def nested_repositories(root):
    """The root and its immediate child directories that are Git repositories."""
    root = Path(root)
    children = sorted(child for child in root.iterdir() if child.is_dir() and not child.is_symlink()
                      and (child / ".git").exists())
    return [root, *children]

