# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Interpretation of output-affecting options forwarded to Core's build and test scripts.

Forwarded arguments always reach the package command unchanged. This module
only reads the ones that decide what gets built and where, so readiness,
artifact selection, and records match the effective build. Repeated options
follow the package command's option parser, where the last occurrence wins.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from ..common.results import ScaffoldError

ANDROID_GN_ARGS = (("is_component_build", "false"), ("enable_android_secondary_abi", "false"),
                   ("use_mold", "false"), ("use_system_xcode", "false"))
CONFIG_TOKENS = ("Debug", "Release", "Component", "Static")
OS_ALIASES = {"mac": "mac", "macos": "mac", "android": "android", "ios": "ios", "linux": "linux",
              "win": "win", "windows": "win"}
VALUE_OPTIONS = ("--target_os", "--target_arch", "--target", "--channel", "--target_android_output_format")
INFO_FLAGS = ("-h", "--help", "-V", "--version")
# Core turns `--ninja <key>:<value>` into `-<key> <value>` for Ninja. These keys make Ninja report or rewrite
# without compiling: a dry run, a tool such as `clean` or `targets`, help, and the version.
NINJA_NOT_COMPILING = ("n", "t", "h", "version")
NINJA_LEAVES_OUTPUT = ("n", "h", "version")


@dataclass
class Forwarded:
    target_os: str | None = None
    target_arch: str | None = None
    build_dir: str | None = None
    ninja_directory: bool = False
    build_config: str | None = None
    target: str | None = None
    channel: str | None = None
    offline: bool = False
    remoteexec: bool | None = None
    info_only: bool = False
    skips_compilation: str | None = None
    leaves_output: bool = False
    output_format: str | None = None
    gn_keys: set = field(default_factory=set)
    gn_values: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)


def interpret(tokens):
    """Read output-affecting options from forwarded tokens; the tokens stay untouched."""
    found = Forwarded()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in INFO_FLAGS:
            found.info_only = True
        elif token == "--offline":
            found.offline = True
        elif token == "--prepare_only":
            found.skips_compilation = token
            found.leaves_output = True
        elif token == "--ninja" or token.startswith("--ninja="):
            value = token.partition("=")[2] if "=" in token else (tokens[index + 1] if index + 1 < len(tokens) else "")
            index += 0 if "=" in token else 1
            key = value.partition(":")[0]
            if key in ("C", "f"):
                changed = "directory" if key == "C" else "build file"
                found.problems.append("--ninja %s changes Ninja's %s; artifact identity is unresolved" % (key, changed))
                found.ninja_directory = found.ninja_directory or key == "C"
            if key in NINJA_NOT_COMPILING:
                found.skips_compilation = "--ninja %s" % key
                found.leaves_output = found.leaves_output or key in NINJA_LEAVES_OUTPUT
        elif token == "--xcode_gen" or token.startswith("--xcode_gen="):
            found.skips_compilation = "--xcode_gen"
            index += 0 if "=" in token else 1
        elif token.startswith("--use_remoteexec"):
            value = "true"
            if "=" in token:
                value = token.partition("=")[2]
            elif index + 1 < len(tokens) and tokens[index + 1] in ("true", "false"):
                index += 1
                value = tokens[index]
            found.remoteexec = value == "true"
        elif token == "-C" or (token.startswith("-C") and not token.startswith("--") and len(token) > 2):
            if token == "-C":
                if index + 1 < len(tokens):
                    index += 1
                    found.build_dir = tokens[index]
                else:
                    found.problems.append("-C has no value")
            else:
                found.build_dir = token[2:]
        elif token == "--gn" or token.startswith("--gn="):
            value = token.partition("=")[2] if "=" in token else (tokens[index + 1] if index + 1 < len(tokens) else "")
            index += 0 if "=" in token else 1
            key, _, setting = value.partition(":")
            found.gn_keys.add(key)
            found.gn_values[key] = setting.strip().strip('"\'')
        elif token.split("=")[0] in VALUE_OPTIONS:
            name, equals, value = token.partition("=")
            if not equals:
                if index + 1 < len(tokens):
                    index += 1
                    value = tokens[index]
                else:
                    found.problems.append("%s has no value" % name)
                    value = None
            if value is not None:
                key = name.lstrip("-")
                setattr(found, "output_format" if key == "target_android_output_format" else key, value)
        elif token in CONFIG_TOKENS:
            found.build_config = token
        index += 1
    return found


def normalize_os(value):
    if value == "host_os":
        return "mac" if platform.system() == "Darwin" else None
    return OS_ALIASES.get(value)


def normalize_arch(value):
    if value == "host_cpu":
        machine = platform.machine().lower()
        return "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("x86_64", "amd64") else machine
    return value


@dataclass
class Effective:
    target: str
    configuration: str
    arch: str
    output_dir: Path | None
    preparation_dir: Path
    build_dir_arg: str | None
    generated: list
    forwarded: list
    build_target: str | None
    channel: str | None
    offline: bool
    sources: dict
    unresolved: list
    changes_output: bool = True
    chosen_gn_keys: frozenset = frozenset()


def conflict(field_name, scaffold_value, forwarded_value, example):
    return ScaffoldError(
        "SELECTOR_CONFLICT",
        "%s is %s from the scaffold options but %s from the forwarded arguments." % (
            field_name, scaffold_value, forwarded_value),
        details={"scaffold": scaffold_value, "forwarded": forwarded_value, "example": example})


def resolve_output_dir(src, build_dir):
    """Core's build scripts resolve a relative -C beneath Chromium's src/out."""
    if build_dir is None:
        return None
    path = Path(build_dir)
    return path if path.is_absolute() else Path(os.path.normpath(src / "out" / build_dir))


def default_build_dir(target, configuration, arch, tests=False):
    """Directory name (under src/out) Core's build script uses by default.

    Android tests build in their own directory so they never change an app build's output.
    """
    name = configuration if arch == "x64" else "%s_%s" % (configuration, arch)
    if target == "android" and tests:
        return "android_tests_" + name
    return name if target == "mac" else "%s_%s" % (target, name)


def resolve_effective(src, forwarded_tokens, target, configuration, explicit_target, explicit_configuration,
                      explicit_offline, default_arch="arm64", tests=False):
    """Combine scaffold selections with interpreted forwarded options.

    Explicit scaffold selections that disagree with forwarded ones fail before
    anything runs. Forwarded values override scaffold defaults, and the
    corresponding generated argument is left out so the child sees one choice.
    """
    fwd = interpret(forwarded_tokens)
    sources = {"target": "scaffold", "configuration": "scaffold", "arch": "default", "output": "default"}
    effective_target = target
    if fwd.target_os is not None:
        os_name = normalize_os(fwd.target_os)
        if os_name is None:
            raise ScaffoldError("UNSUPPORTED_CAPABILITY", "--target_os=%s is not supported here." % fwd.target_os)
        if explicit_target and os_name != explicit_target:
            raise conflict("The target", explicit_target, os_name, "bcore build %s" % os_name)
        effective_target, sources["target"] = os_name, "forwarded"
    effective_configuration = configuration
    if fwd.build_config is not None:
        forwarded_config = fwd.build_config.lower()
        if explicit_configuration and forwarded_config != explicit_configuration.lower():
            raise conflict("The configuration", explicit_configuration, fwd.build_config,
                           "bcore build --configuration %s" % forwarded_config)
        effective_configuration, sources["configuration"] = fwd.build_config, "forwarded"
    arch = default_arch
    if fwd.target_arch is not None:
        arch, sources["arch"] = normalize_arch(fwd.target_arch), "forwarded"
    if explicit_offline and (fwd.remoteexec is True):
        raise conflict("Compilation mode", "--offline", "--use_remoteexec=true", "bcore build --offline")
    generated = []
    if fwd.target_os is None:
        generated.append("--target_os=%s" % effective_target)
    if fwd.target_arch is None:
        generated.append("--target_arch=%s" % arch)
    default_dir = default_build_dir(effective_target, effective_configuration, arch, tests)
    build_dir_arg = fwd.build_dir if fwd.build_dir is not None else default_dir
    if fwd.build_dir is None:
        generated.extend(["-C", default_dir])
    else:
        sources["output"] = "forwarded"
    if fwd.build_config is None:
        generated.append(effective_configuration)
    if effective_target == "android":
        if fwd.output_format is None:
            generated.append("--target_android_output_format=apk")
        for key, value in ANDROID_GN_ARGS:
            if key not in fwd.gn_keys:
                generated.append("--gn=%s:%s" % (key, value))
    if effective_configuration == "Release" and fwd.channel is None:
        generated.append("--channel=release")
    offline = explicit_offline or fwd.offline or fwd.remoteexec is False
    if not fwd.offline and fwd.remoteexec is None:
        generated.append("--offline" if explicit_offline else "--use_remoteexec=true")
    unresolved = list(fwd.problems)
    for key, expected, normalize in (("target_os", effective_target, normalize_os),
                                     ("target_cpu", arch, normalize_arch)):
        if key in fwd.gn_values and normalize(fwd.gn_values[key]) != expected:
            if key == "target_os" and explicit_target:
                raise conflict("The target", explicit_target, fwd.gn_values[key], "bcore build %s" % explicit_target)
            unresolved.append("--gn %s:%s overrides the %s this build is recorded under (%s)" % (
                key, fwd.gn_values[key], key, expected))
    if fwd.info_only:
        unresolved.append("the forwarded arguments ask the package command for information, not a build")
    if fwd.skips_compilation:
        unresolved.append("%s makes the build exit without compiling the application" % fwd.skips_compilation)
    if fwd.build_dir is not None and not fwd.build_dir:
        unresolved.append("-C has an empty value")
    if effective_target == "android" and fwd.output_format not in (None, "apk"):
        unresolved.append("the output format %r does not produce an APK" % fwd.output_format)
    if fwd.target and fwd.target != "brave":
        unresolved.append("build target %r does not produce the application" % fwd.target)
    output = resolve_output_dir(src, build_dir_arg)
    if fwd.ninja_directory:
        sources["output"] = "unresolved Ninja directory"
    return Effective(target=effective_target, configuration=effective_configuration, arch=arch,
                     output_dir=None if fwd.ninja_directory else output,
                     preparation_dir=output, build_dir_arg=build_dir_arg, generated=generated, forwarded=list(forwarded_tokens),
                     build_target=fwd.target, channel=fwd.channel, offline=offline, sources=sources,
                     unresolved=unresolved, changes_output=not fwd.leaves_output,
                     chosen_gn_keys=frozenset(fwd.gn_keys | ({"use_remoteexec"} if fwd.remoteexec is not None
                                                             else set())))
