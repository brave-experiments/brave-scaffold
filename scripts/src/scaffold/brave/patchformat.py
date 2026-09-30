# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which files a patch writes, whatever its header prefix is called."""

from __future__ import annotations

import re

DIFF_GIT_LINE = re.compile(r"^diff --git (\S+) (\S+)$")
RENAME_LINE = re.compile(r"^(?:rename|copy) (?:from|to) (\S+)$")


class UnknownPatchFormat(ValueError):
    pass


def _strip_prefix(name):
    """Drop the first path component, as `git apply` does by default, whatever the prefix is called."""
    if name.startswith('"') or "/" not in name:
        raise UnknownPatchFormat("unsupported path %r" % name)
    return name.split("/", 1)[1]


def parse_patch_targets(text):
    """Repository-relative files a patch writes. Raises UnknownPatchFormat instead of guessing."""
    targets = set()
    for line in text.splitlines():
        if line.startswith(("--- ", "+++ ")):
            name = line[4:].split("\t")[0].strip()
            if name and name != "/dev/null":
                targets.add(_strip_prefix(name))
        elif line.startswith("diff --git "):
            match = DIFF_GIT_LINE.match(line)
            if match is None:
                raise UnknownPatchFormat("unsupported header %r" % line)
            targets.update(_strip_prefix(name) for name in match.groups())
        elif RENAME_LINE.match(line):
            targets.add(RENAME_LINE.match(line).group(1))
    if not targets:
        raise UnknownPatchFormat("no file headers")
    return targets
