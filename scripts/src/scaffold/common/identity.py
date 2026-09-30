# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkout discovery, Git layout validation, and selection."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .results import ScaffoldError, repair

CORE_PACKAGE_NAME = "brave-core"


@dataclass(frozen=True)
class CheckoutIdentity:
    core: Path
    src: Path
    workspace: Path
    outer: Path
    selection_source: str
    alias: str | None = None
    record: object = None
    requested: str | None = None

    @property
    def direnv_dir(self):
        return self.record.direnv_dir if self.record else None

    def to_context(self):
        return {"checkout": str(self.core), "alias": self.alias,
                "selection_source": self.selection_source}


def looks_like_path(token):
    return os.sep in token or token.startswith((".", "~")) or os.path.isabs(token)


def is_core(path):
    package = Path(path) / "package.json"
    if path.name != "brave" or not package.is_file():
        return False
    try:
        return json.loads(package.read_text(encoding="utf-8")).get("name") == CORE_PACKAGE_NAME
    except (OSError, ValueError, AttributeError):
        return False


def _candidates_at(directory):
    """Core directories directly implied by one directory, without descending far."""
    if is_core(directory):
        return [directory]
    for relative in ("brave", "src/brave"):
        if is_core(directory / relative):
            return [directory / relative]
    scm = directory / "_bad_scm"
    if scm.is_dir():
        found = []
        for workspace in sorted(scm.iterdir()):
            if is_core(workspace / "src" / "brave"):
                found.append(workspace / "src" / "brave")
        return found
    return []


def discover_core(start):
    """Find the Core directory implied by a path, walking up. Returns a list (0, 1, or many)."""
    start = Path(os.path.realpath(start))
    for directory in (start, *start.parents):
        found = _candidates_at(directory)
        if found:
            return found
    return []


def layout_of(core):
    core = Path(os.path.realpath(core))
    src = core.parent
    workspace = src.parent
    outer = workspace.parent.parent if workspace.parent.name == "_bad_scm" else workspace
    return src, workspace, outer


# --- Git layout -----------------------------------------------------------------


def linked_worktree(repo):
    """Return a description when `repo` is a Git linked worktree, else None.

    Uses canonical Git directory and common directory identity read from
    metadata. A `.git` file or detached HEAD alone is not evidence.
    """
    marker = Path(repo) / ".git"
    if marker.is_dir() or not marker.is_file():
        return None
    try:
        first = marker.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    git_dir = Path(first[len("gitdir:"):].strip())
    if not git_dir.is_absolute():
        git_dir = Path(repo) / git_dir
    git_dir = Path(os.path.realpath(git_dir))
    common_file = git_dir / "commondir"
    if not common_file.is_file():
        return None
    try:
        common = Path(common_file.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    if not common.is_absolute():
        common = git_dir / common
    common = Path(os.path.realpath(common))
    if common == git_dir:
        return None
    return {"repository": str(repo), "git_dir": str(git_dir), "common_dir": str(common)}


def find_linked_worktrees(core, src, outer):
    found = []
    for role, repo in (("core", core), ("chromium", src), ("outer", outer)):
        info = linked_worktree(repo)
        if info:
            info["role"] = role
            found.append(info)
    return found


def unsupported_worktree_error(worktrees, core):
    roles = ", ".join(item["role"] for item in worktrees)
    return ScaffoldError(
        "UNSUPPORTED_CAPABILITY",
        "The selected checkout uses Git linked worktrees (%s). Brave browser development "
        "needs a separate full checkout for each branch or workspace." % roles,
        details={"core": str(core), "worktrees": worktrees},
        repairs=[repair(["bdev", "context", "--checkout", "<path-to-a-full-checkout>"],
                        note="Select an existing full checkout; nothing was moved or changed.")])


# --- Selection ------------------------------------------------------------------


def _record_for(config, core):
    real = Path(os.path.realpath(core))
    for record in config.checkouts:
        if record.core_real == real:
            return record
    return None


def _candidate_list(config):
    return [record.alias or str(record.core) for record in config.checkouts]


def build_identity(core, config, source, requested=None, alias=None):
    src, workspace, outer = layout_of(core)
    record = _record_for(config, core)
    return CheckoutIdentity(core=Path(os.path.realpath(core)), src=src, workspace=workspace,
                            outer=outer, selection_source=source,
                            alias=record.alias if record else alias, record=record,
                            requested=requested)


def resolve_selector(config, selector, cwd):
    if not looks_like_path(selector):
        for record in config.checkouts:
            if record.alias == selector:
                return build_identity(record.core_real, config, "explicit_alias", selector)
        raise ScaffoldError(
            "CHECKOUT_NOT_FOUND", "No checkout has the alias %r." % selector,
            details={"candidates": _candidate_list(config)},
            repairs=[repair(["bdev", "checkout", "list"])])
    path = Path(os.path.expanduser(selector))
    if not path.is_absolute():
        path = Path(cwd) / path
    found = discover_core(path) if path.exists() else []
    if not found:
        raise ScaffoldError(
            "CHECKOUT_NOT_FOUND", "No Brave Core checkout was found at or above %s." % path,
            details={"path": str(path)},
            repairs=[repair(["bdev", "checkout", "list"])])
    if len(found) > 1:
        raise ambiguous(found, selector)
    return build_identity(found[0], config, "explicit_path", selector)


def ambiguous(found, where):
    return ScaffoldError(
        "CHECKOUT_AMBIGUOUS",
        "%s contains more than one Brave source workspace; name the Core directory." % where,
        details={"candidates": [str(item) for item in found]},
        repairs=[repair(["bdev", "context", "--checkout", str(found[0])],
                        note="Example only; choose the checkout you intend to use.")])


def select_checkout(config, selector=None, cwd=None, required=True):
    """Explicit selector first, then the enclosing checkout of the caller's cwd."""
    cwd = cwd or os.getcwd()
    if selector:
        return resolve_selector(config, selector, cwd)
    found = discover_core(cwd)
    if len(found) > 1:
        raise ambiguous(found, cwd)
    if found:
        return build_identity(found[0], config, "cwd")
    if not required:
        return None
    candidates = _candidate_list(config)
    example = str(config.checkouts[0].core) if config.checkouts else "/path/to/src/brave"
    raise ScaffoldError(
        "CHECKOUT_REQUIRED",
        "No checkout is selected: the current directory is not inside a Brave checkout.",
        details={"candidates": candidates, "example": "--checkout %s" % example},
        repairs=[repair(["bdev", "checkout", "list"])])


def validate_layout(identity):
    """Check the Core/Chromium/outer relationship and reject linked worktrees."""
    worktrees = find_linked_worktrees(identity.core, identity.src, identity.outer)
    if worktrees:
        raise unsupported_worktree_error(worktrees, identity.core)
    if not is_core(identity.core):
        raise ScaffoldError("CHECKOUT_NOT_FOUND",
                            "%s is not a Brave Core directory." % identity.core)
    if identity.src.name != "src":
        raise ScaffoldError(
            "CHECKOUT_NOT_FOUND",
            "Core must live in a Chromium 'src' directory; found parent %s." % identity.src)
