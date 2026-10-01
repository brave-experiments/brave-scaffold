# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Source fingerprints used to say whether an output reflects the current code."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from ..common.procs import run_capture
from ..common.redaction import redact_argv
from ..common.results import ScaffoldError
from . import gitstate

EVIDENCE_BYTES = 64 << 20
UNKNOWN_MESSAGE = "Build freshness is unknown; this output may not include the latest code."


def resolve_head(repo, log=None):
    """Commit id of a repository's HEAD (asked of Git; see `gitstate.head_commit`)."""
    return gitstate.head_commit(repo, log)


def _file_signature(path):
    try:
        info = os.stat(path)
    except OSError:
        return "missing"
    return "%d:%d" % (info.st_size, info.st_mtime_ns)


def worktree_state(repo, log=None):
    """Hash of a repository's uncommitted state, including the content signature of changed files."""
    result = run_capture(["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                         str(repo), None, log, timeout=120, max_bytes=EVIDENCE_BYTES)
    if result.returncode != 0 or result.truncated:
        return None
    digest = hashlib.sha256(result.stdout.encode())
    for entry in sorted(item for item in result.stdout.split("\0") if item):
        digest.update(_file_signature(Path(repo) / entry[3:]).encode())
    return digest.hexdigest()


def tracked_changes_state(repo, log=None, timeout=90):
    """Hash of the tracked files that differ from HEAD, with each file's size and modification time.

    Untracked files and other repositories nested inside are not covered. None when Git cannot answer in time,
    so a slow or failing check makes freshness unknown instead of claiming a match.
    """
    result = run_capture(["git", "--no-optional-locks", "-C", str(repo), "diff-index", "-z", "--name-only", "HEAD",
                          "--"], str(repo), None, log, timeout=timeout, max_bytes=EVIDENCE_BYTES)
    if result.returncode != 0 or result.timed_out or result.truncated:
        return None
    digest = hashlib.sha256()
    for name in sorted(item for item in result.stdout.split("\0") if item):
        digest.update(("%s=%s\n" % (name, _file_signature(Path(repo) / name))).encode())
    return digest.hexdigest()


def dependency_state(identity, log=None):
    """(revisions, changes) digests over the dependency repositories gclient manages, or (None, None).

    Covers each repository's HEAD and its tracked changes with each changed file's size and modification time.
    None when the repository list is unreadable or any repository cannot be inspected completely, so
    unchecked dependencies make freshness unknown instead of current.
    """
    from . import sync_scope
    scope = sync_scope.sync_repositories(identity)
    if not scope.complete:
        return None, None
    heads, changes = hashlib.sha256(), hashlib.sha256()
    repositories = sorted(item for item in scope.repositories if item not in (identity.src, identity.core))
    for index, repo in enumerate(repositories, 1):
        if log:
            log.progress("Checking source state: dependency %d/%d (%s)" % (
                index, len(repositories), os.path.relpath(repo, identity.src)))
        try:
            head, changed = gitstate.tracked_snapshot(repo, log)
        except ScaffoldError:
            return None, None
        label = os.path.relpath(repo, identity.src)
        heads.update(("%s=%s\n" % (label, head)).encode())
        for name in sorted(changed):
            changes.update(("%s/%s=%s\n" % (label, name, _file_signature(repo / name))).encode())
    return heads.hexdigest(), changes.hexdigest()


INCLUDE_ENV = re.compile(r"^include_env=([^#]+)(?:#.*)?$")


def env_files_fingerprint(core):
    """Hash of Core's .env and every file it includes with include_env, however deeply."""
    root = Path(core) / ".env"
    if not root.is_file():
        return "absent"
    digest, seen = hashlib.sha256(), set()

    def visit(path):
        real = os.path.realpath(path)
        if real in seen:
            digest.update(("cycle:%s\n" % real).encode())
            return
        seen.add(real)
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            digest.update(("unreadable:%s\n" % real).encode())
            return
        digest.update(("file:%s\n%s\n" % (real, text)).encode())
        for line in text.split("\n"):
            match = INCLUDE_ENV.match(line)
            if match:
                visit(Path(path).parent / match.group(1).strip())

    visit(root)
    return digest.hexdigest()


def compute(identity, patched_paths, effective_args, log=None, extra=None):
    """Fingerprint of the inputs a build depends on. Parts that could not be computed are None.

    `extra` adds inputs that only apply to some outputs (for example Android support).
    """
    if log:
        log.phase("Checking source state...")
    patched = hashlib.sha256()
    for repo_path in sorted(patched_paths):
        patched.update(("%s=%s\n" % (repo_path, _file_signature(identity.src / repo_path))).encode())
    dependency_heads, dependency_changes = dependency_state(identity, log)
    return {
        "core_head": resolve_head(identity.core, log),
        "chromium_head": resolve_head(identity.src, log),
        "core_worktree": worktree_state(identity.core, log),
        "chromium_worktree": tracked_changes_state(identity.src, log),
        "dependency_heads": dependency_heads,
        "dependency_changes": dependency_changes,
        "patched_files": patched.hexdigest() if patched_paths else None,
        "env_file": env_files_fingerprint(identity.core),
        "build_arguments": hashlib.sha256("\0".join(redact_argv(effective_args)).encode()).hexdigest(),
        **(extra or {}),
    }


TRACKED = ("core_head", "chromium_head", "core_worktree", "chromium_worktree", "dependency_heads",
           "dependency_changes", "patched_files", "env_file",
           "support_head", "support_worktree", "support_resources")
NOT_TRACKED = "untracked files are not tracked, and neither are files the build reads from outside the checkout"


def assess(recorded, current, output_state):
    """Return {"status": current|stale|unknown, "evidence": [...]} for one output."""
    evidence = []
    if output_state.needs_revalidation:
        attempt = output_state.last_uncertain_attempt() or {}
        evidence.append("An attempt to change this output did not complete successfully (%s); its contents may be "
                        "partly overwritten." % attempt.get("outcome", "unknown"))
    if not recorded:
        evidence.append("No successful build of this output was recorded by the scaffold.")
        return {"status": "unknown", "evidence": evidence}
    predating = [key for key in TRACKED if key in current and key not in recorded]
    if predating:
        evidence.append("The recorded build predates the comparison of: " + ", ".join(predating))
        return {"status": "unknown", "evidence": evidence}
    changed, unchecked = [], []
    for key in TRACKED:
        if key not in recorded and key not in current:
            continue
        before, now = recorded.get(key), current.get(key)
        if before is None or now is None:
            unchecked.append(key)
        elif before != now:
            changed.append(key)
    if changed:
        evidence.append("Inputs changed since the recorded build: " + ", ".join(changed))
        return {"status": "stale", "evidence": evidence}
    if unchecked:
        evidence.append("Could not compare: " + ", ".join(unchecked))
        return {"status": "unknown", "evidence": evidence}
    if output_state.needs_revalidation:
        return {"status": "unknown", "evidence": evidence}
    evidence.append("Tracked inputs match the recorded build (%s)." % NOT_TRACKED)
    return {"status": "current", "evidence": evidence}
