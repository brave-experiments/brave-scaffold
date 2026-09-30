# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Scaffold configuration: parsing, validation, and minimal in-place edits."""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .results import ScaffoldError, repair

SCHEMA_VERSION = 1
CONFIG_NAME = "brave-scaffold.toml"
ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
PLATFORM_NAMES = ("mac", "macos", "android")
TOP_FIELDS = {"schema_version", "logging", "defaults", "checkouts"}
LOGGING_FIELDS = {"commands"}
DEFAULT_FIELDS = {"platform", "android_device"}
CHECKOUT_FIELDS = {"alias", "core", "direnv_dir"}
EXAMPLE = """[[checkouts]]
alias = "main"
core = "/work/browser/_bad_scm/workspace/src/brave"
direnv_dir = "environments/main\""""


def scaffold_root():
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class CheckoutRecord:
    core: str
    core_real: Path
    alias: str | None = None
    direnv_dir: Path | None = None
    direnv_dir_text: str | None = None


@dataclass
class Config:
    path: Path
    exists: bool
    commands_logging: bool = True
    default_platform: str | None = None
    default_android_device: str | None = None
    checkouts: list = field(default_factory=list)

    @property
    def directory(self):
        return self.path.parent


def _invalid(config_path, field_path, message, example=None):
    details = {"file": str(config_path), "field": field_path}
    if example:
        details["example"] = example
    return ScaffoldError("CONFIG_INVALID", "%s: %s" % (field_path, message), details=details)


def default_config_path(root=None):
    return (root or scaffold_root()) / CONFIG_NAME


def load_config(path=None, root=None, explicit=False):
    """Load configuration. A missing default file yields an empty configuration."""
    path = Path(path) if path else default_config_path(root)
    path = path.expanduser()
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    path = Path(os.path.normpath(path))
    if not path.exists():
        if explicit:
            raise ScaffoldError(
                "CONFIG_INVALID", "Configuration file does not exist: %s" % path,
                details={"file": str(path)},
                repairs=[repair(["cp", str(default_config_path(root).with_name("brave-scaffold.example.toml")),
                                 str(path)], note="Copy the example, then replace its placeholder path.")])
        return Config(path=path, exists=False)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _invalid(path, "file", "cannot be read as TOML (%s)" % error, "schema_version = 1")
    return _validate(path, data)


def _expect_table(config_path, data, name, allowed):
    if not isinstance(data, dict):
        raise _invalid(config_path, name, "must be a table")
    for key in data:
        if key not in allowed:
            raise _invalid(config_path, "%s.%s" % (name, key) if name != "top level" else key,
                           "is not a supported field; supported fields: %s" % ", ".join(sorted(allowed)))


def _validate(path, data):
    _expect_table(path, data, "top level", TOP_FIELDS)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise _invalid(path, "schema_version", "must be %d" % SCHEMA_VERSION, "schema_version = 1")
    config = Config(path=path, exists=True)
    if "logging" in data:
        _expect_table(path, data["logging"], "logging", LOGGING_FIELDS)
        value = data["logging"].get("commands", True)
        if not isinstance(value, bool):
            raise _invalid(path, "logging.commands", "must be true or false", "[logging]\ncommands = true")
        config.commands_logging = value
    if "defaults" in data:
        _expect_table(path, data["defaults"], "defaults", DEFAULT_FIELDS)
        platform = data["defaults"].get("platform")
        if platform is not None:
            if not isinstance(platform, str) or platform.lower() not in PLATFORM_NAMES:
                raise _invalid(path, "defaults.platform",
                               "must be one of: %s" % ", ".join(PLATFORM_NAMES),
                               '[defaults]\nplatform = "mac"')
            config.default_platform = platform.lower()
        device = data["defaults"].get("android_device")
        if device is not None:
            if not isinstance(device, str) or not device:
                raise _invalid(path, "defaults.android_device", "must be a device id string",
                               '[defaults]\nandroid_device = "emulator-5554"')
            config.default_android_device = device
    raw = data.get("checkouts", [])
    if not isinstance(raw, list):
        raise _invalid(path, "checkouts", "must be an array of tables", EXAMPLE)
    seen_alias, seen_core, seen_env = {}, {}, {}
    for index, entry in enumerate(raw):
        where = "checkouts[%d]" % index
        _expect_table(path, entry, where, CHECKOUT_FIELDS)
        core = entry.get("core")
        if not isinstance(core, str) or not os.path.isabs(core):
            raise _invalid(path, where + ".core", "is required and must be an absolute path to src/brave",
                           EXAMPLE)
        alias = entry.get("alias")
        if alias is not None and (not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias)):
            raise _invalid(path, where + ".alias",
                           "must start with a letter or digit and contain only letters, digits, - and _",
                           EXAMPLE)
        env_text = entry.get("direnv_dir")
        env_dir = None
        if env_text is not None:
            if not isinstance(env_text, str) or not env_text:
                raise _invalid(path, where + ".direnv_dir", "must be a directory path", EXAMPLE)
            env_dir = Path(env_text).expanduser()
            if not env_dir.is_absolute():
                env_dir = path.parent / env_dir
            env_dir = Path(os.path.normpath(env_dir))
        core_real = Path(os.path.realpath(core))
        for table, key, label in ((seen_alias, alias, "alias"), (seen_core, core_real, "core"),
                                  (seen_env, env_dir, "direnv_dir")):
            if key is None:
                continue
            if key in table:
                raise _invalid(path, "%s.%s" % (where, label),
                               "duplicates %s of checkouts[%d]; each checkout needs its own value"
                               % (label, table[key]), EXAMPLE)
            table[key] = index
        config.checkouts.append(CheckoutRecord(core=core, core_real=core_real, alias=alias,
                                               direnv_dir=env_dir, direnv_dir_text=env_text))
    return config


def _block_ranges(lines):
    """Line ranges [start, end) of each [[checkouts]] table."""
    ranges, start = [], None
    for index, line in enumerate(lines):
        header = re.match(r"\s*\[", line)
        if start is not None and header:
            ranges.append((start, index))
            start = None
        if re.match(r"\s*\[\[checkouts\]\]", line):
            start = index
    if start is not None:
        ranges.append((start, len(lines)))
    return ranges


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def upsert_checkout(path, core, alias=None, direnv_dir=None):
    """Add or update the single record for a Core path without disturbing other text.

    Existing values are never replaced silently: a different alias or environment
    directory is reported as a conflict.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else "schema_version = 1\n"
    core_real = Path(os.path.realpath(core))
    lines = text.splitlines(keepends=True)
    match = None
    for start, end in _block_ranges(lines):
        found = re.search(r'^\s*core\s*=\s*"([^"]*)"', "".join(lines[start:end]), re.MULTILINE)
        if found and Path(os.path.realpath(found.group(1))) == core_real:
            match = (start, end)
    if match is None:
        block = ["[[checkouts]]\n"]
        if alias:
            block.append('alias = "%s"\n' % alias)
        block.append('core = "%s"\n' % core_real)
        if direnv_dir:
            block.append('direnv_dir = "%s"\n' % direnv_dir)
        prefix = "" if not text or text.endswith("\n") else "\n"
        atomic_write(path, text + prefix + "\n" + "".join(block))
        return "added"
    start, end = match
    changed = False
    for key, value in (("alias", alias), ("direnv_dir", direnv_dir)):
        if value is None:
            continue
        body = "".join(lines[start:end])
        existing = re.search(r'^\s*%s\s*=\s*"([^"]*)"' % key, body, re.MULTILINE)
        if existing:
            if existing.group(1) != value:
                raise ScaffoldError(
                    "CONFIG_INVALID",
                    "The checkout already has %s = %r; edit %s to change it deliberately."
                    % (key, existing.group(1), path),
                    details={"file": str(path), "field": key})
            continue
        core_line = next(i for i in range(start, end) if re.match(r"\s*core\s*=", lines[i]))
        lines.insert(core_line + 1, '%s = "%s"\n' % (key, value))
        end += 1
        changed = True
    if changed:
        atomic_write(path, "".join(lines))
        return "updated"
    return "unchanged"
