# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which repositories Core patches, the patches each one holds, and what their metadata records.

Core lists the patched repositories in `patches/.repositories.cfg`; each entry names a directory under
Chromium's source root (`//` is Chromium itself) whose patches live in the matching directory under
`patches/`. Metadata paths and patch targets are relative to the repository that owns the patch. Paths in
this module's public results are relative to Chromium's source root unless a name says otherwise.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .patchformat import UnknownPatchFormat, parse_patch_targets

REPOSITORIES_FILE = ".repositories.cfg"
SCHEMA_VERSION = 1

NO_METADATA = "no metadata"
METADATA_OUTDATED = "metadata unreadable or in another schema version"
PATCH_CHANGED = "patch file changed"
PATCH_REMOVED = "patch file removed"
TARGET_CHANGED = "target file changed"
TARGET_MISSING = "target file missing"


def sha256_file(path):
    """The SHA-256 of a regular file. A named pipe would block the open and a device may never end, so neither is read."""
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_or_none(path):
    try:
        return sha256_file(path)
    except OSError:
        return None


@dataclass(frozen=True)
class PatchRepository:
    rel: str  # "" for Chromium's own source root, otherwise a path under it such as "v8"
    path: Path
    patch_dir: Path

    def source_path(self, relative):
        return posixpath.join(self.rel, relative) if self.rel else relative


@dataclass
class PatchEntry:
    """One patch and its metadata. Either file may be missing."""

    repository: PatchRepository
    patch: Path
    info: Path
    has_patch: bool
    has_info: bool
    metadata_problems: list = field(default_factory=list)
    recorded: dict = field(default_factory=dict)  # repository-relative path -> checksum, trusted entries only
    patch_checksum: str | None = None
    targets: set | None = None  # repository-relative paths the current patch writes; None when unreadable
    target_problem: str | None = None

    @property
    def name(self):
        return self.patch.name

    @property
    def metadata_usable(self):
        return self.has_info and not self.metadata_problems

    def write_set(self):
        """Every repository-relative path applying this patch can reset or write: current and earlier targets."""
        return set(self.targets or ()) | set(self.recorded)

    def source_paths(self, relative_paths):
        return {self.repository.source_path(item) for item in relative_paths}

    def stale_reason(self):
        """Why Core would apply this patch again, or None when it treats it as applied.

        The decision mirrors Core's own: missing or unusable metadata, a changed or removed patch file, or a
        target that is missing or no longer holds the recorded content.
        """
        if not self.has_patch:
            return PATCH_REMOVED
        if not self.has_info:
            return NO_METADATA
        if self.metadata_problems:
            return METADATA_OUTDATED
        if sha256_or_none(self.patch) != self.patch_checksum:
            return PATCH_CHANGED
        for relative, checksum in self.recorded.items():
            current = sha256_or_none(self.repository.path / relative)
            if current is None:
                return TARGET_MISSING
            if current != checksum:
                return TARGET_CHANGED
        return None


@dataclass
class PatchInventory:
    repositories: list
    entries: list
    problems: list = field(default_factory=list)  # evidence that could not be established

    @property
    def complete(self):
        return not self.problems


def parse_repositories(text):
    """Repository paths under the source root listed in `.repositories.cfg`, as Core reads them."""
    found = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not line.startswith("//"):
            raise ValueError("line %d: %r must start with '//'" % (number, line))
        relative = line[2:].strip("/")
        if ".." in relative.split("/"):
            raise ValueError("line %d: %r must stay under '//'" % (number, line))
        if relative in found:
            raise ValueError("line %d: %r is listed twice" % (number, line))
        found.append(relative)
    if "" not in found:
        raise ValueError("Chromium's own '//' is not listed")
    return found


def patched_repositories(identity):
    """(repositories, problems) from Core's list of patched repositories.

    Without the list only Chromium's own source root is known; patch files elsewhere are then reported
    because their owning repository cannot be established.
    """
    patches = identity.core / "patches"
    listing = patches / REPOSITORIES_FILE
    try:
        text = listing.read_text(encoding="utf-8")
    except FileNotFoundError:
        relatives, problems = [""], []
        if any(patches.glob("*/**/*.patch*")):
            problems.append("patches/%s is missing, so the repositories that hold the patches in subdirectories "
                            "are unknown" % REPOSITORIES_FILE)
    except (OSError, UnicodeDecodeError) as error:
        return [], ["patches/%s cannot be read: %s" % (REPOSITORIES_FILE, error)]
    else:
        try:
            relatives, problems = parse_repositories(text), []
        except ValueError as error:
            return [], ["patches/%s is malformed: %s" % (REPOSITORIES_FILE, error)]
    return [PatchRepository(relative, identity.src.joinpath(*relative.split("/")) if relative else identity.src,
                            patches.joinpath(*relative.split("/")) if relative else patches)
            for relative in relatives], problems


def _valid_relative(path):
    return (isinstance(path, str) and path and not path.startswith("/") and "\0" not in path
            and ".." not in path.split("/") and "." not in path.split("/"))


def read_metadata(entry):
    """Fill `recorded`, `patch_checksum`, and `metadata_problems` from a patch's metadata file.

    Every entry must name a relative path and a checksum. One bad entry makes all of this patch's metadata
    untrusted, because the entries that survived no longer show what the patch writes.
    """
    try:
        info = json.loads(entry.info.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        entry.metadata_problems.append("cannot read %s: %s" % (entry.info.name, error))
        return
    if not isinstance(info, dict):
        entry.metadata_problems.append("%s is not an object" % entry.info.name)
        return
    if info.get("schemaVersion") != SCHEMA_VERSION:
        entry.metadata_problems.append("%s has schema version %r, not %d" % (
            entry.info.name, info.get("schemaVersion"), SCHEMA_VERSION))
    if not isinstance(info.get("patchChecksum"), str) or not info["patchChecksum"]:
        entry.metadata_problems.append("%s has no patch checksum" % entry.info.name)
    else:
        entry.patch_checksum = info["patchChecksum"]
    applies = info.get("appliesTo")
    if not isinstance(applies, list) or not applies:
        entry.metadata_problems.append("%s has no usable appliesTo list" % entry.info.name)
        return
    recorded = {}
    for index, item in enumerate(applies):
        path = item.get("path") if isinstance(item, dict) else None
        checksum = item.get("checksum") if isinstance(item, dict) else None
        if not _valid_relative(path) or not isinstance(checksum, str) or not checksum:
            entry.metadata_problems.append("%s appliesTo entry %d lacks a valid path or checksum" % (
                entry.info.name, index))
            continue
        recorded[path] = checksum
    if not entry.metadata_problems:
        entry.recorded = recorded


def read_targets(entry):
    try:
        entry.targets = parse_patch_targets(entry.patch.read_text(encoding="utf-8", errors="replace"))
    except (OSError, UnknownPatchFormat) as error:
        entry.target_problem = "cannot tell which files %s writes (%s)" % (entry.patch.name, error)


def read_inventory(identity):
    """Every patch in every patched repository, with its metadata and current targets. Read-only."""
    repositories, problems = patched_repositories(identity)
    entries = []
    for repository in repositories:
        try:
            names = sorted(path.name for path in repository.patch_dir.iterdir() if path.is_file())
        except FileNotFoundError:
            continue
        except OSError as error:
            problems.append("%s cannot be listed: %s" % (repository.patch_dir, error))
            continue
        patch_names = {name for name in names if name.endswith(".patch")}
        info_names = {name for name in names if name.endswith(".patchinfo")}
        stems = sorted({name[:-6] for name in patch_names} | {name[:-10] for name in info_names})
        for stem in stems:
            entry = PatchEntry(repository, repository.patch_dir / (stem + ".patch"),
                               repository.patch_dir / (stem + ".patchinfo"),
                               stem + ".patch" in patch_names, stem + ".patchinfo" in info_names)
            if entry.has_info:
                read_metadata(entry)
            if entry.has_patch:
                read_targets(entry)
            entries.append(entry)
    return PatchInventory(repositories, entries, problems)
