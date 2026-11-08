# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Dependency checkouts that Chromium replaces with its own files at the version Core syncs to.

When Chromium turns a dependency (a gitlink checked out by gclient) into files it tracks itself, the old
checkout's files are untracked from Chromium's point of view and `git checkout` of the new version refuses to
overwrite them, so Core's sync fails. A checkout that holds nothing but the pinned commit can be moved aside
into the scaffold's state; one with local work stops the sync with the paths to review.
"""

from __future__ import annotations

import errno
import json
import os
import re
import time
from dataclasses import dataclass

from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair
from . import gitstate
from .records import checkout_key, store_root

GITLINK = "160000"
TAG = re.compile(r"[0-9]+(?:\.[0-9]+){3}")


@dataclass
class Move:
    path: str  # relative to Chromium's source root
    commit: str  # the commit Chromium pins today
    problem: str | None = None  # why it cannot be moved automatically


def _git(repo, args, log, timeout=120):
    return run_capture(["git", "--literal-pathspecs", "-C", str(repo), *args], str(repo), None, log,
                       timeout=timeout, max_bytes=gitstate.EVIDENCE_BYTES)


def target_tag(identity):
    try:
        tag = json.loads((identity.core / "package.json").read_text(encoding="utf-8"))["config"]["projects"]["chrome"]["tag"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return tag if isinstance(tag, str) and TAG.fullmatch(tag) else None


def target_commit(identity, log=None):
    """The commit of the Chromium tag Core pins, when Chromium already has that tag locally."""
    tag = target_tag(identity)
    if tag is None:
        return None
    result = _git(identity.src, ["rev-parse", "--verify", "--quiet", "refs/tags/%s^{commit}" % tag], log, timeout=30)
    text = result.stdout.strip()
    return text if result.returncode == 0 and not result.truncated and text else None


def _replaced_gitlinks(src, head, target, log):
    """{path: pinned commit} for gitlinks at `head` that `target` replaces with files or directories."""
    diff = _git(src, ["diff-tree", "-r", "-z", "--no-renames", head, target], log)
    if diff.returncode != 0 or diff.truncated:
        return {}
    removed, fields = {}, diff.stdout.split("\0")
    for meta, path in zip(fields[0::2], fields[1::2]):
        parts = meta.lstrip(":").split()
        if len(parts) >= 3 and parts[0] == GITLINK and parts[1] != GITLINK:
            removed[path] = parts[2]
    if not removed:
        return {}
    listing = _git(src, ["ls-tree", "-z", target, "--", *removed], log)
    if listing.returncode != 0 or listing.truncated:
        return {}
    present = {entry.partition("\t")[2] for entry in listing.stdout.split("\0") if entry}
    return {path: commit for path, commit in removed.items() if path in present}


def _problem(repo, commit, log):
    if not (repo / ".git").exists():
        return "is not a Git checkout"
    head = gitstate.head_commit(repo, log)
    if head != commit:
        return "is at %s, not the pinned %s" % (head[:12] if head else "an unreadable commit", commit[:12])
    try:
        if gitstate.local_changes(repo, log):
            return "has local changes"
    except ScaffoldError:
        return "could not be inspected"
    if _git(repo, ["rev-parse", "--verify", "--quiet", "refs/stash"], log, timeout=30).returncode == 0:
        return "has stashed work"
    local = _git(repo, ["rev-list", "-n", "1", "--branches", "--not", "--remotes"], log, timeout=60)
    if local.returncode != 0 or local.stdout.strip():
        return "has commits that no remote branch contains"
    return None


def find(identity, log=None):
    """Dependency checkouts in the way of Chromium's checkout of the tag Core pins.

    Empty when the tag is not available locally, Chromium is already at it, or nothing is in the way.
    """
    head = gitstate.head_commit(identity.src, log)
    target = target_commit(identity, log)
    if head is None or target is None or head == target:
        return []
    moves = []
    for path, commit in sorted(_replaced_gitlinks(identity.src, head, target, log).items()):
        repo = identity.src / path
        if repo.is_dir() and not repo.is_symlink() and any(repo.iterdir()):
            moves.append(Move(path, commit, _problem(repo, commit, log)))
    return moves


def destination_root(identity, state_root):
    return store_root(state_root) / "moved-dependencies" / checkout_key(identity.core)


def blocked_error(identity, moves, state_root):
    blocked = [move for move in moves if move.problem]
    base = destination_root(identity, state_root)
    return ScaffoldError(
        "PREPARATION_CONFLICT",
        "Chromium %s replaces %d dependency checkout(s) with its own files, and these hold work that a move "
        "could hide: %s. Nothing was moved or synced." % (
            target_tag(identity), len(blocked), "; ".join("%s %s" % (move.path, move.problem) for move in blocked)),
        details={"dependencies": [{"path": move.path, "pinned": move.commit, "problem": move.problem}
                                  for move in blocked], "phase": "sync"},
        repairs=[repair(["mv", str(identity.src / move.path), str(base / "manual" / move.path)],
                        note="Keep any work you need from it first; the sync then writes Chromium's files here.")
                 for move in blocked])


def move_aside(ctx, identity, moves):
    """Move unchanged checkouts into the scaffold's state; returns what moved. Raises before moving anything
    if any checkout has local work."""
    if not moves:
        return []
    if any(move.problem for move in moves):
        raise blocked_error(identity, moves, ctx.state_root)
    base = destination_root(identity, ctx.state_root) / time.strftime("%Y%m%dT%H%M%S")
    moved = []
    for move in moves:
        source, destination = identity.src / move.path, base / move.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, destination)
        except OSError as error:
            raise ScaffoldError(
                "PREPARATION_CONFLICT", "Could not move %s to %s (%s); nothing was synced." % (
                    source, destination, "different file system" if error.errno == errno.EXDEV else error.strerror),
                details={"moved": moved, "phase": "sync"}) from error
        ctx.log.phase("Moved %s to %s: Chromium %s tracks these files itself." % (
            move.path, destination, target_tag(identity)))
        moved.append({"path": move.path, "pinned": move.commit, "moved_to": str(destination)})
    return moved
