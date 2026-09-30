# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Source fingerprints used to say whether an output reflects the current code."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..common.procs import run_capture
from ..common.redaction import redact_argv

UNKNOWN_MESSAGE = "Build freshness is unknown; this output may not include the latest code."


def resolve_head(repo):
    """Commit id of a repository's HEAD, read from Git metadata (no subprocess)."""
    git = Path(repo) / ".git"
    if git.is_file():
        try:
            line = git.read_text(encoding="utf-8").splitlines()[0]
            git = Path(line.split(":", 1)[1].strip())
        except (OSError, IndexError):
            return None
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head
    ref = head[4:].strip()
    try:
        return (git / ref).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    common = git / "commondir"
    packed = (Path(os.path.realpath(git / common.read_text().strip())) if common.is_file() else git) / "packed-refs"
    try:
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        pass
    return None


def _file_signature(path):
    try:
        info = os.stat(path)
    except OSError:
        return "missing"
    return "%d:%d" % (info.st_size, info.st_mtime_ns)


def core_worktree_state(identity, log=None):
    """Hash of Core's uncommitted state, including the content signature of changed files."""
    result = run_capture(["git", "-C", str(identity.core), "status", "--porcelain=v1", "-z",
                          "--untracked-files=all"], str(identity.core), None, log, timeout=120)
    if result.returncode != 0:
        return None
    digest = hashlib.sha256(result.stdout.encode())
    for entry in sorted(item for item in result.stdout.split("\0") if item):
        digest.update(_file_signature(identity.core / entry[3:]).encode())
    return digest.hexdigest()


def compute(identity, patched_paths, effective_args, log=None):
    """Fingerprint of the inputs a build depends on. Missing parts are recorded as None."""
    patched = hashlib.sha256()
    for repo_path in sorted(patched_paths):
        patched.update(("%s=%s\n" % (repo_path, _file_signature(identity.src / repo_path))).encode())
    env_file = identity.core / ".env"
    return {
        "core_head": resolve_head(identity.core),
        "chromium_head": resolve_head(identity.src),
        "core_worktree": core_worktree_state(identity, log),
        "patched_files": patched.hexdigest() if patched_paths else None,
        "env_file": hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file.is_file() else "absent",
        "build_arguments": hashlib.sha256("\0".join(redact_argv(effective_args)).encode()).hexdigest(),
    }


TRACKED = ("core_head", "chromium_head", "core_worktree", "patched_files", "env_file")


def assess(recorded, current, output_state):
    """Return {"status": current|stale|unknown, "evidence": [...]} for one output."""
    evidence = []
    if output_state.needs_revalidation:
        attempt = output_state.last_attempt() or {}
        evidence.append("An attempt to change this output did not complete successfully (%s); its contents may be "
                        "partly overwritten." % attempt.get("outcome", "unknown"))
    if not recorded:
        evidence.append("No successful build of this output was recorded by the scaffold.")
        return {"status": "unknown", "evidence": evidence}
    changed, unchecked = [], []
    for key in TRACKED:
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
    evidence.append("Tracked inputs match the recorded build (Chromium files outside patched paths are not tracked).")
    return {"status": "current", "evidence": evidence}
