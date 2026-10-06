# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Identify the installed scaffold without requiring Git metadata."""

import os
import subprocess

from .config import scaffold_root


def read_revision():
    root = scaffold_root()
    unknown = {"sha": None, "commit_date": None, "dirty": None}
    # An unpacked copy must not inherit a containing repository's identity.
    if not (root / ".git").exists():
        return unknown
    environ = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environ["GIT_OPTIONAL_LOCKS"] = "0"

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], env=environ,
                              capture_output=True, text=True, check=True, timeout=2).stdout.strip()

    try:
        sha, commit_date = git("show", "-s", "--format=%H%n%cI", "HEAD").splitlines()
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return unknown
    return {"sha": sha, "commit_date": commit_date, "dirty": dirty}


def format_revision(revision):
    if revision["sha"] is None:
        return "brave-scaffold unknown"
    return "brave-scaffold %s (%s)%s" % (
        revision["sha"][:12], revision["commit_date"][:10], " dirty" if revision["dirty"] else "")
