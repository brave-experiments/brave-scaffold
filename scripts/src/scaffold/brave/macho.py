# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read the dynamic-library references of Mach-O files to tell whether an application bundle is self-contained.

Only the load commands are read (a few kilobytes per file), never whole binaries.
"""

from __future__ import annotations

import ctypes
import functools
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
CPU_TYPE_ARM64 = 0x0100000C
CPU_TYPES = {"arm64": CPU_TYPE_ARM64, "x64": 0x01000007}
MH_EXECUTE = 2
LC_RPATH = 0x8000001C
DYLIB_COMMANDS = {0xC: "load", 0x20: "lazy", 0x80000018: "weak", 0x8000001F: "reexport", 0x80000023: "upward"}
# Libraries the system provides; they live in the shared cache rather than on disk.
SYSTEM_PREFIXES = ("/System/", "/usr/lib/")


@dataclass
class Image:
    path: Path
    filetype: int
    dylibs: list = field(default_factory=list)  # (name, kind)
    rpaths: list = field(default_factory=list)


def _slice_offset(handle, magic_bytes, wanted=CPU_TYPE_ARM64):
    """Offset of the slice for `wanted` in a universal file (else its first slice), or of a thin file's header."""
    magic = struct.unpack(">I", magic_bytes)[0]
    if magic not in (FAT_MAGIC, FAT_MAGIC_64):
        return 0
    count = struct.unpack(">I", handle.read(4))[0]
    entry_size, layout = (32, ">iiQQI") if magic == FAT_MAGIC_64 else (20, ">iiIII")
    offsets = []
    for _ in range(min(count, 32)):
        cputype, _subtype, offset, _size, _align = struct.unpack(layout, handle.read(entry_size)[:struct.calcsize(layout)])
        offsets.append((cputype, offset))
    if not offsets:
        return None
    return next((offset for found, offset in offsets if found == wanted), offsets[0][1])


def read_image(path, arch="arm64"):
    """The dylib references and run paths of one 64-bit Mach-O file, or None for any other file."""
    try:
        with open(path, "rb") as handle:
            magic_bytes = handle.read(4)
            if len(magic_bytes) < 4:
                return None
            offset = _slice_offset(handle, magic_bytes, CPU_TYPES.get(arch, CPU_TYPE_ARM64))
            if offset is None:
                return None
            handle.seek(offset)
            header = handle.read(32)
            if len(header) < 32 or struct.unpack("<I", header[:4])[0] != MH_MAGIC_64:
                return None
            _magic, _cpu, _sub, filetype, ncmds, sizeofcmds, _flags, _reserved = struct.unpack("<IiiIIIII", header)
            commands = handle.read(sizeofcmds)
    except (OSError, struct.error):
        # A short or truncated file, such as a Java class file, which shares the universal-file magic number.
        return None
    image = Image(Path(path), filetype)
    position = 0
    for _ in range(ncmds):
        if position + 8 > len(commands):
            break
        cmd, size = struct.unpack_from("<II", commands, position)
        if size < 8:
            break
        body = commands[position:position + size]
        if cmd in DYLIB_COMMANDS or cmd == LC_RPATH:
            name_offset = struct.unpack_from("<I", body, 8)[0] if len(body) >= 12 else size
            name = body[name_offset:].split(b"\0", 1)[0].decode("utf-8", "replace")
            if cmd == LC_RPATH:
                image.rpaths.append(name)
            else:
                image.dylibs.append((name, DYLIB_COMMANDS[cmd]))
        position += size
    return image


def bundle_images(bundle, arch="arm64"):
    """Every Mach-O file inside the bundle; symbolic links are not followed."""
    images = []
    for directory, _dirs, files in os.walk(bundle):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                image = read_image(path, arch)
                if image is not None:
                    images.append(image)
    return images


@functools.cache
def _shared_cache_lookup():
    """dyld's own test for a library held in the shared cache rather than on disk (macOS 11 and later)."""
    try:
        function = ctypes.CDLL("/usr/lib/libSystem.B.dylib")._dyld_shared_cache_contains_path
    except (OSError, AttributeError):
        return None
    function.restype, function.argtypes = ctypes.c_bool, [ctypes.c_char_p]
    return function


def _system_library(path):
    """Whether a system-path candidate is a library this Mac provides, on disk or in the shared cache."""
    if os.path.exists(path):
        return True
    lookup = _shared_cache_lookup()
    return bool(lookup and lookup(os.fsencode(os.path.normpath(path))))


def _inside(path, root):
    resolved = os.path.realpath(path)
    return resolved == root or resolved.startswith(root + os.sep)


def _expand(value, owner, executable_dirs):
    """`@loader_path` is the directory of the file that holds the path, whether a load command or a run path."""
    if value.startswith("@loader_path"):
        return [str(owner.path.parent) + value[len("@loader_path"):]]
    if value.startswith("@executable_path"):
        return [str(directory) + value[len("@executable_path"):] for directory in executable_dirs]
    return [value] if value.startswith("/") else []


def _candidates(name, image, executable_dirs, rpaths):
    """Paths a reference may resolve to; `rpaths` are (run path, the image that holds it) pairs."""
    if name.startswith("@rpath/"):
        rest = name[len("@rpath/"):]
        return [os.path.join(base, rest) for rpath, owner in rpaths for base in _expand(rpath, owner, executable_dirs)]
    return _expand(name, image, executable_dirs)


def outside_references(bundle, arch="arm64"):
    """Dylib references in the bundle that resolve outside it, as a list of dictionaries.

    `@rpath` references are searched with the loading file's run paths and those of the bundle's executables,
    an approximation of dyld's search along the load chain. System libraries are allowed. A weak reference
    that resolves nowhere is allowed, because dyld skips it.
    """
    root = os.path.realpath(bundle)
    images = bundle_images(bundle, arch)
    executables = [image for image in images if image.filetype == MH_EXECUTE]
    executable_dirs = sorted({image.path.parent for image in executables}) or [Path(bundle) / "Contents" / "MacOS"]
    executable_rpaths = [(rpath, image) for image in executables for rpath in image.rpaths]
    problems = []
    for image in images:
        rpaths = [*((rpath, image) for rpath in image.rpaths), *executable_rpaths]
        for name, kind in image.dylibs:
            if name.startswith(SYSTEM_PREFIXES):
                continue
            candidates = _candidates(name, image, executable_dirs, rpaths)
            existing = [candidate for candidate in candidates if os.path.exists(candidate)]
            if any(_inside(candidate, root) for candidate in existing):
                continue
            # A system candidate counts only when the system provides that library, usually from the shared
            # cache, and no non-system file outside the bundle exists that dyld could load instead.
            system = [candidate for candidate in candidates if os.path.normpath(candidate).startswith(SYSTEM_PREFIXES)]
            outside = [candidate for candidate in existing if candidate not in system]
            if not outside and (any(_system_library(candidate) for candidate in system) or
                                (not existing and kind == "weak")):
                continue
            problems.append({"file": os.path.relpath(image.path, bundle), "dylib": name,
                             "resolves_to": list(dict.fromkeys(os.path.realpath(item) for item in existing))[:3]})
    return problems
