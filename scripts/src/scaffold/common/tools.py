# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkout-local Node, package manager, and Python resolution.

Resolution reads the checkout's own payload directories and never starts the
checkout's shim launchers, which can download or fall back to global tools.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .checks import BLOCKER, PASS, WARNING, CheckResult
from .env import depot_tools_repair, find_depot_tools, resolves_inside
from .platforms import host_architecture, host_platform
from .procs import run_capture
from .results import ScaffoldError, repair

PROBE = Path(__file__).with_name("payload_probe.py")
SUPPORTED_MANAGERS = ("npm", "pnpm")


@dataclass
class PackageDeclaration:
    manager: str
    manager_range: str | None
    node_range: str | None
    declared: bool


@dataclass
class Toolchain:
    manager: str
    node: Path
    node_bin: Path
    manager_entry: Path
    depot_tools: Path
    vpython3: Path
    node_version: str
    manager_version: str
    declaration: PackageDeclaration
    freshness: dict = field(default_factory=dict)

    def describe(self):
        return {"package_manager": self.manager, "package_manager_version": self.manager_version,
                "package_manager_entry": str(self.manager_entry), "node": str(self.node),
                "node_version": self.node_version, "vpython3": str(self.vpython3),
                "depot_tools": str(self.depot_tools), "payload_freshness": self.freshness}


# --- package.json ---------------------------------------------------------------


def _declaration_error(package, message):
    return ScaffoldError(
        "DEPENDENCY_INCOMPATIBLE", "%s: %s" % (package, message),
        details={"file": str(package), "supported_package_managers": list(SUPPORTED_MANAGERS)})


def read_declaration(core):
    package = Path(core) / "package.json"
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise _declaration_error(package, "cannot be read (%s)" % error)
    engines = data.get("devEngines") if isinstance(data, dict) else None
    if isinstance(engines, dict):
        runtime = engines.get("runtime")
        node_range = runtime.get("version") if isinstance(runtime, dict) and runtime.get("name") == "node" else None
        manager = engines.get("packageManager")
        if manager is None:
            return PackageDeclaration("npm", None, node_range, declared=False)
        if not isinstance(manager, dict) or not isinstance(manager.get("name"), str):
            raise _declaration_error(package, "devEngines.packageManager must be an object with a name")
        name = manager["name"]
        if name not in SUPPORTED_MANAGERS:
            raise _declaration_error(package, "package manager %r is not supported" % name)
        return PackageDeclaration(name, manager.get("version"), node_range, declared=True)
    if engines is not None:
        raise _declaration_error(package, "devEngines must be an object")
    return PackageDeclaration("npm", None, None, declared=False)


# --- version ranges ---------------------------------------------------------------

_VERSION = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text):
    found = _VERSION.match(text.strip())
    if not found:
        return None
    return tuple(int(part or 0) for part in found.groups())


def satisfies(version, range_text):
    """Evaluate the common npm range forms. Returns True, False, or None if unsupported."""
    have = parse_version(version)
    if have is None:
        return None
    for alternative in range_text.split("||"):
        outcome = _all_comparators(have, _comparators(alternative))
        if outcome is None:
            return None
        if outcome:
            return True
    return False


def _comparators(alternative):
    text = re.sub(r"(\S+)\s+-\s+(\S+)", r">=\1 <=\2", alternative.strip())
    return re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", text).split()


def _bump(parts):
    return (*parts[:-1], parts[-1] + 1, *(0,) * (3 - len(parts)))


def _all_comparators(have, comparators):
    for comparator in comparators:
        found = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?v?((?:\d+|[xX*])(?:\.(?:\d+|[xX*])){0,2})", comparator)
        if not found:
            return None
        ok = _within(have, found.group(1) or "=", found.group(2))
        if not ok:
            return False
    return True


def _within(have, op, text):
    parts = []
    for part in text.split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    count = len(parts)
    if count == 0:
        return op not in (">", "<")
    base = (*parts, *(0,) * (3 - count))
    if op == ">=":
        return have >= base
    if op == "<=":
        return have <= base if count == 3 else have < _bump(parts)
    if op == ">":
        return have > base if count == 3 else have >= _bump(parts)
    if op == "<":
        return have < base
    if op == "=":
        return have == base if count == 3 else base <= have < _bump(parts)
    if op == "~":
        return base <= have < (_bump(parts[:1]) if count == 1 else _bump(parts[:2]))
    if parts[0] > 0 or count == 1:
        upper = _bump(parts[:1])
    elif parts[1] > 0 or count == 2:
        upper = _bump(parts[:2])
    else:
        upper = _bump(parts)
    return base <= have < upper


# --- payload layout ---------------------------------------------------------------


def versioned_node_payload(core, arch):
    """The versioned npm payload declared by a checkout's EXTRA_DEPS installer.

    Read only literal metadata. Importing the installer would load depot_tools
    and could run setup code during inspection.
    """
    installer = Path(core) / "tools/cr/install_extra_deps.py"
    key = "src/brave/third_party/node/" + ("mac_arm64" if arch == "arm64" else "mac")
    if not resolves_inside(installer, core):
        return None
    try:
        tree = ast.parse(installer.read_text(encoding="utf-8"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "EXTRA_DEPS" for target in node.targets))
        entry = ast.literal_eval(assignment.value)[key]
        objects = entry["objects"]
        if len(objects) != 1 or "overlayed_on" in objects[0]:
            return None
        archive, digest = objects[0]["object_name"], objects[0]["sha256sum"]
        match = re.fullmatch(r"node-(v\d+\.\d+\.\d+)-darwin-" + arch + r"\.tar\.gz", archive)
        condition = 'host_os == "mac" and host_cpu == "%s"' % arch
        if match is None or not re.fullmatch(r"[a-f0-9]{64}", digest) or entry.get("condition") != condition:
            return None
    except (OSError, UnicodeError, SyntaxError, StopIteration, ValueError, KeyError, TypeError):
        return None
    destination = Path(core) / "third_party/node" / ("mac_arm64" if arch == "arm64" else "mac")
    stamp = destination / ("." + archive.replace(".", "_") + "_hash.stamp")
    return {"node_dir": destination / archive.removesuffix(".tar.gz"), "node_entry_key": key,
            "installer": installer, "stamp": stamp, "sha256": digest, "version": match.group(1)}


def node_layout(core, manager):
    """Payload paths for the observed bootstrap layout on this host."""
    if host_platform() != "mac":
        raise ScaffoldError("UNSUPPORTED_CAPABILITY",
                            "Checkout-local tool resolution is available on macOS hosts only.")
    suffix = "mac-arm64" if host_architecture() == "arm64" else "mac-x64"
    base = Path(core) / "third_party" / "node"
    legacy = versioned_node_payload(core, "arm64" if suffix == "mac-arm64" else "x64") \
        if manager == "npm" and not (Path(core) / "tools/cr/extra_deps.py").exists() else None
    node_dir = legacy["node_dir"] if legacy else base / ("node-" + suffix)
    return {
        "node_dir": node_dir,
        "node": node_dir / "bin" / "node",
        "node_bin": node_dir / "bin",
        "npm_entry": node_dir / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        "npm_package": node_dir / "lib" / "node_modules" / "npm" / "package.json",
        "pnpm_entry": base / "node_modules" / "pnpm" / "bin" / "pnpm.mjs",
        "pnpm_package": base / "node_modules" / "pnpm" / "package.json",
        "node_entry_key": legacy["node_entry_key"] if legacy else "src/brave/third_party/node/node-" + suffix,
        "installer": legacy["installer"] if legacy else Path(core) / "tools/cr/tarball_installer.py",
        "versioned_payload": legacy,
        "pnpm_entry_key": "src/brave/third_party/node/node_modules",
    }


def payload_escapes(identity, entries):
    """An installation destination that resolves outside the frozen Core root, or None."""
    destinations = [Path(identity.core) / "third_party/node", *(identity.workspace / key for key in entries)]
    return next((os.path.realpath(path) for path in destinations
                 if os.path.lexists(path) and not resolves_inside(path, identity.core)), None)


def _package_version(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def payload_entries(layout, manager):
    """The (label, installer entry) pairs the package manager needs deployed.

    Node always matters. The package manager entry is separate only for pnpm; npm ships inside the Node payload,
    so another manager's entry is never required for it. Inspection and repair both read this list.
    """
    entries = [("node", layout["node_entry_key"])]
    if manager == "pnpm":
        entries.append(("package_manager", layout["pnpm_entry_key"]))
    return entries


def payload_freshness(identity, layout, manager, log=None):
    """Ask the checkout's payload metadata whether the entries the package manager needs are deployed. Read-only."""
    legacy = layout["versioned_payload"]
    if legacy:
        try:
            deployed = resolves_inside(legacy["stamp"], identity.core) and \
                legacy["stamp"].read_text(encoding="utf-8").strip() == legacy["sha256"]
        except (OSError, UnicodeError):
            deployed = False
        return {"node": "current" if deployed else "stale"}
    entries = payload_entries(layout, manager)
    result = {}
    for label, key in entries:
        probe = run_capture([sys.executable, "-B", "-I", str(PROBE), str(identity.core), str(identity.workspace), key],
                            str(identity.core), {"PATH": os.environ.get("PATH", "")}, log, timeout=60)
        state = "unverified"
        if probe.returncode == 0:
            try:
                state = json.loads(probe.stdout).get("state", "unverified")
            except ValueError:
                state = "unverified"
        result[label] = state
    return result


def tools_setup_command(identity):
    return ["bcore", "tools", "setup", "--checkout", str(identity.core)]


def inspect_toolchain(identity, log=None):
    """Read-only inspection of checkout-local tools.

    Returns (toolchain_or_None, checks). Never installs, updates, or launches a
    shim. The only processes started are the checkout's own Node binary for its
    version and a read-only payload metadata probe.
    """
    checks = []
    repair_step = repair(tools_setup_command(identity),
                         note="Explicit repair of checkout-local tools; run only when authorized.")

    def add(name, status, summary, **evidence):
        checks.append(CheckResult(name=name, status=status, summary=summary, scopes=("tools",),
                                  evidence=evidence, affects=("package commands",),
                                  repairs=[repair_step] if status != PASS else []))
        return checks[-1]

    try:
        declaration = read_declaration(identity.core)
    except ScaffoldError as error:
        add("package-manager-declaration", BLOCKER, error.message)
        return None, checks
    add("package-manager-declaration", PASS,
        "%s%s" % (declaration.manager, "" if declaration.declared else " (no declaration; older npm checkout)"),
        manager=declaration.manager, version_range=declaration.manager_range)

    depot = find_depot_tools(identity)
    if depot is None:
        add("vpython3", BLOCKER, "No checkout-local vpython3 was found under vendor/depot_tools or third_party/depot_tools.")
        checks[-1].repairs = [depot_tools_repair(identity)]
    else:
        add("vpython3", PASS, "Checkout-local vpython3", path=str(depot / "vpython3"))

    try:
        layout = node_layout(identity.core, declaration.manager)
    except ScaffoldError as error:
        add("local-node", BLOCKER, error.message)
        return None, checks
    if not os.access(layout["node"], os.X_OK):
        add("local-node", BLOCKER, "Checkout-local Node is missing: %s" % layout["node"], path=str(layout["node"]))
        node_version = None
    elif not resolves_inside(layout["node"], identity.core):
        add("local-node", BLOCKER, "Node at %s resolves outside the checkout (%s)." % (
            layout["node"], os.path.realpath(layout["node"])), path=str(layout["node"]))
        node_version = None
    else:
        version = run_capture([str(layout["node"]), "--version"], str(identity.core),
                              {"PATH": os.environ.get("PATH", "")}, log, timeout=30)
        node_version = version.stdout.strip() if version.returncode == 0 else None
        if node_version is None:
            add("local-node", BLOCKER, "Checkout-local Node did not report a version.", path=str(layout["node"]))
        elif layout["versioned_payload"] and node_version != layout["versioned_payload"]["version"]:
            add("local-node", BLOCKER, "Node %s differs from the pinned archive version %s." % (
                node_version, layout["versioned_payload"]["version"]), path=str(layout["node"]), version=node_version)
        elif declaration.node_range:
            verdict = satisfies(node_version, declaration.node_range)
            if verdict is True:
                add("local-node", PASS, "Node %s satisfies %s" % (node_version, declaration.node_range),
                    path=str(layout["node"]), version=node_version)
            elif verdict is False:
                add("local-node", BLOCKER, "Node %s does not satisfy %s (stale payload)" % (
                    node_version, declaration.node_range), path=str(layout["node"]), version=node_version)
            else:
                add("local-node", BLOCKER, "Cannot verify Node %s against %r." % (node_version, declaration.node_range),
                    path=str(layout["node"]))
        else:
            add("local-node", PASS, "Node %s" % node_version, path=str(layout["node"]), version=node_version)

    if declaration.manager == "pnpm":
        entry, package = layout["pnpm_entry"], layout["pnpm_package"]
    else:
        entry, package = layout["npm_entry"], layout["npm_package"]
    manager_version = _package_version(package) if entry.is_file() else None
    if not entry.is_file() or manager_version is None:
        add("local-package-manager", BLOCKER, "Checkout-local %s is missing: %s" % (declaration.manager, entry),
            path=str(entry))
    elif not (resolves_inside(entry, identity.core) and resolves_inside(package, identity.core)):
        add("local-package-manager", BLOCKER, "%s at %s resolves outside the checkout (%s)." % (
            declaration.manager, entry, os.path.realpath(entry)), path=str(entry))
    elif declaration.manager_range and satisfies(manager_version, declaration.manager_range) is not True:
        verdict = satisfies(manager_version, declaration.manager_range)
        add("local-package-manager", BLOCKER,
            ("%s %s does not satisfy %s (stale payload)" if verdict is False
             else "Cannot verify %s %s against %s") % (declaration.manager, manager_version, declaration.manager_range),
            path=str(entry), version=manager_version)
    else:
        add("local-package-manager", PASS, "%s %s" % (declaration.manager, manager_version),
            path=str(entry), version=manager_version)

    freshness = payload_freshness(identity, layout, declaration.manager, log)
    stale = [label for label, state in freshness.items() if state == "stale"]
    unverified = [label for label, state in freshness.items() if state != "current" and label not in stale]
    if stale:
        add("payload-freshness", BLOCKER, "Payload metadata reports stale or undeployed: %s" % ", ".join(stale),
            **freshness)
    elif unverified:
        installer = layout["installer"]
        checks.append(CheckResult(
            name="payload-freshness", status=BLOCKER, scopes=("tools",), evidence=freshness,
            summary="The pinned payload of %s cannot be verified: this checkout's payload metadata "
                    "(tools/cr/extra_deps.py) is missing or unreadable, and a compatible version alone does not "
                    "prove the pinned payload." % " and ".join(unverified),
            affects=("package commands",), repairs=[repair_step] if installer.is_file() else []))
    else:
        add("payload-freshness", PASS, "Payload metadata matches the checkout's pinned versions.", **freshness)

    if any(check.status == BLOCKER for check in checks):
        return None, checks
    toolchain = Toolchain(
        manager=declaration.manager, node=layout["node"], node_bin=layout["node_bin"], manager_entry=entry,
        depot_tools=depot, vpython3=depot / "vpython3", node_version=node_version,
        manager_version=manager_version, declaration=declaration, freshness=freshness)
    return toolchain, checks


def require_toolchain(identity, log=None):
    toolchain, checks = inspect_toolchain(identity, log)
    if toolchain is None:
        failing = [check for check in checks if check.status == BLOCKER]
        raise ScaffoldError(
            "LOCAL_TOOL_MISSING",
            "Checkout-local tools are not ready: %s" % "; ".join(check.summary for check in failing),
            details={"checks": [check.to_dict() for check in failing], "checkout": str(identity.core)},
            repairs=failing[0].repairs if failing and failing[0].repairs else [])
    return toolchain, checks


def package_argv(toolchain, arguments):
    """The exact argv for a package command, with older-npm separator translation."""
    arguments = list(arguments)
    if toolchain.manager == "npm" and len(arguments) >= 3 and arguments[0] == "run" and arguments[2] != "--":
        arguments = arguments[:2] + ["--"] + arguments[2:]
    return [str(toolchain.node), str(toolchain.manager_entry), *arguments]


def child_environment(loaded, toolchain, shim_dir=None):
    """Final environment for package commands: local tools first on PATH."""
    env = dict(loaded)
    entries = [str(toolchain.node_bin), str(toolchain.depot_tools)]
    if shim_dir:
        entries.insert(0, str(shim_dir))
    rest = [item for item in loaded.get("PATH", "").split(os.pathsep) if item and item not in entries]
    env["PATH"] = os.pathsep.join([*entries, *rest])
    env["VPYTHON3"] = str(toolchain.vpython3)
    return env
