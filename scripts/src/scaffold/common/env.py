# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkout environment: pure exports, direnv approval, and explicit loading."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import scaffold_root
from .procs import run_capture
from .results import ScaffoldError, repair

# Variables owned by the tooling. They are removed from the inherited environment
# before the chosen environment loads, so stale exports cannot pick a checkout.
OWNED_SELECTORS = (
    "BRAVE_BROWSER_DIR", "BRAVE_SRC_ROOT", "BRAVE_CORE_DIR", "BRAVE_DEPOT_TOOLS_DIR",
    "VPYTHON3", "BRAVE_PYTHONPATH_DIR", "BRAVE_ACTIVE_PYTHONPATH_DIR",
    "BRAVE_LAUNCHER_CHECKOUT_DIR",
)
ENVIRONMENT_DUMP = Path(__file__).with_name("envdump.py")


@dataclass(frozen=True)
class DerivedEnv:
    exports: dict
    depot_tools: Path
    vpython3: Path
    pythonpath_dir: Path


def resolves_inside(path, root):
    """Whether `path` (after following every link) lies within `root` (also resolved).

    `root` must be the frozen checkout directory, never a directory found inside the checkout that could itself
    be a link: a payload directory linked from outside would otherwise move both sides of the comparison.
    """
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def depot_tools_repair(identity):
    """Environment loading needs local Python, so a scaffold sync cannot restore it."""
    destination = identity.core / "vendor/depot_tools"
    alternate = identity.src / "third_party/depot_tools"
    return repair([], requires_user_action=True,
                  note="Restore checkout-local depot_tools manually at %s or %s. Preserve local work first. "
                       "For an existing depot_tools Git repository, restore its missing vpython3 from its own HEAD. "
                       "If neither directory is a repository, use Core's standalone setup to create a local "
                       "depot_tools checkout. Scaffold sync and tools setup cannot load the environment until "
                       "local vpython3 is restored; do not substitute a global interpreter." % (destination, alternate))


def find_depot_tools(identity):
    """The checkout's own depot_tools directory, anchored in the frozen Chromium source root."""
    for candidate in (identity.core / "vendor" / "depot_tools", identity.src / "third_party" / "depot_tools"):
        if os.access(candidate / "vpython3", os.X_OK) and resolves_inside(candidate / "vpython3", identity.src):
            return candidate
    return None


def require_depot_tools(identity):
    depot = find_depot_tools(identity)
    if depot is None:
        raise ScaffoldError(
            "LOCAL_TOOL_MISSING",
            "No checkout-local vpython3 exists under %s or %s. An environment cannot supply one from outside the "
            "checkout." % (identity.core / "vendor" / "depot_tools", identity.src / "third_party" / "depot_tools"),
            details={"tool": "vpython3", "checkout": str(identity.core)},
            repairs=[depot_tools_repair(identity)])
    return depot


def _remove_entry(value, entry):
    if not value:
        return []
    return [item for item in value.split(os.pathsep) if item and item != entry]


def derive_env(identity, environ, with_pythonpath=False):
    """Compute checkout exports from identity alone. Never runs anything."""
    depot = require_depot_tools(identity)
    pythonpath_dir = identity.core / "script"
    path_entries = _remove_entry(environ.get("PATH", ""), environ.get("BRAVE_DEPOT_TOOLS_DIR", ""))
    path_entries = [item for item in path_entries if item != str(depot)]
    exports = {
        "BRAVE_BROWSER_DIR": str(identity.outer),
        "BRAVE_SRC_ROOT": str(identity.src),
        "BRAVE_CORE_DIR": str(identity.core),
        "BRAVE_DEPOT_TOOLS_DIR": str(depot),
        "VPYTHON3": str(depot / "vpython3"),
        "BRAVE_PYTHONPATH_DIR": str(pythonpath_dir),
        "PATH": os.pathsep.join([str(depot), *path_entries]),
    }
    if with_pythonpath:
        previous = environ.get("BRAVE_ACTIVE_PYTHONPATH_DIR", "")
        entries = [item for item in _remove_entry(environ.get("PYTHONPATH", ""), previous)
                   if item != str(pythonpath_dir)]
        exports["BRAVE_ACTIVE_PYTHONPATH_DIR"] = str(pythonpath_dir)
        exports["PYTHONPATH"] = os.pathsep.join([str(pythonpath_dir), *entries])
    return DerivedEnv(exports=exports, depot_tools=depot, vpython3=depot / "vpython3",
                      pythonpath_dir=pythonpath_dir)


def format_bash(derived, environ, with_pythonpath):
    lines = ["export %s=%s" % (name, shlex.quote(value)) for name, value in derived.exports.items()]
    if not with_pythonpath and environ.get("BRAVE_ACTIVE_PYTHONPATH_DIR"):
        remaining = _remove_entry(environ.get("PYTHONPATH", ""), environ["BRAVE_ACTIVE_PYTHONPATH_DIR"])
        lines.append("export PYTHONPATH=%s" % shlex.quote(os.pathsep.join(remaining)) if remaining
                     else "unset PYTHONPATH")
        lines.append("unset BRAVE_ACTIVE_PYTHONPATH_DIR")
    return "\n".join(lines) + "\n"


# --- Environment files ------------------------------------------------------------


GENERATED_MARKER = "Generated by `bdev env init`"
HEADER = """# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""


def envrc_text(identity, config_path, with_pythonpath=False):
    bdev = scaffold_root() / "scripts" / "bdev"
    command = [str(bdev), "env", "export", "--config", str(config_path), "--checkout",
               str(identity.core), "--format", "bash"]
    if with_pythonpath:
        command.append("--with-pythonpath")
    return HEADER + (
        "\n# " + GENERATED_MARKER + ". Exports the source and tool paths of one Brave\n"
        "# checkout. It performs no installs, syncs, builds, or source preparation.\n"
        "exports=$(%s) || {\n"
        "  echo 'brave-scaffold: could not derive the checkout environment' >&2\n"
        "  exit 1\n"
        "}\n"
        "eval \"$exports\"\n" % " ".join(shlex.quote(part) for part in command))


def envrc_path(directory):
    return Path(directory) / ".envrc"


# --- direnv ---------------------------------------------------------------------


def clean_environ(environ):
    """Inherited environment without tool-owned selectors and direnv state."""
    cleaned = {key: value for key, value in environ.items()
               if key == "DIRENV_CONFIG" or (key not in OWNED_SELECTORS and not key.startswith("DIRENV_"))}
    depot = environ.get("BRAVE_DEPOT_TOOLS_DIR", "")
    if depot:
        cleaned["PATH"] = os.pathsep.join(_remove_entry(environ.get("PATH", ""), depot))
    active = environ.get("BRAVE_ACTIVE_PYTHONPATH_DIR", "")
    if active and "PYTHONPATH" in cleaned:
        remaining = _remove_entry(cleaned["PYTHONPATH"], active)
        if remaining:
            cleaned["PYTHONPATH"] = os.pathsep.join(remaining)
        else:
            del cleaned["PYTHONPATH"]
    return cleaned


def find_direnv(environ):
    return shutil.which("direnv", path=environ.get("PATH"))


def direnv_status(direnv, directory, environ, log=None):
    """Approval state of exactly `<directory>/.envrc`. Returns a dict."""
    rc = envrc_path(directory)
    state = {"path": str(rc), "exists": rc.is_file(), "allowed": None, "found_path": None}
    if not rc.is_file():
        return state
    result = run_capture([direnv, "status", "--json"], str(directory), clean_environ(environ), log)
    if result.returncode != 0:
        state["error"] = (result.stderr or "direnv status failed").strip()
        return state
    try:
        found = (json.loads(result.stdout).get("state") or {}).get("foundRC") or {}
    except ValueError:
        state["error"] = "direnv status returned unreadable output"
        return state
    state["found_path"] = found.get("path")
    allowed = found.get("allowed")
    if isinstance(allowed, bool):
        state["allowed"] = allowed
    elif isinstance(allowed, int):
        state["allowed"] = allowed == 0
    same = state["found_path"] and os.path.realpath(state["found_path"]) == os.path.realpath(rc)
    if not same:
        state["allowed"] = None
        state["error"] = "direnv resolved a different .envrc (%s); a parent file is not a substitute" % (
            state["found_path"] or "none")
    return state


def require_environment(identity, environ, log=None):
    """Return (direnv, directory) for a checkout whose environment is approved."""
    record = identity.record
    if record is None or record.direnv_dir is None:
        raise ScaffoldError(
            "ENVIRONMENT_REQUIRED",
            "This checkout has no scaffold environment configured.",
            details={"checkout": str(identity.core)},
            repairs=[repair(["bdev", "checkout", "add", "<name>", str(identity.core)]),
                     repair(["bdev", "env", "init", "--checkout", str(identity.core)])])
    directory = Path(os.path.realpath(record.direnv_dir))
    direnv = find_direnv(environ)
    if direnv is None:
        raise ScaffoldError(
            "ENVIRONMENT_REQUIRED", "direnv is not installed; the checkout environment needs it.",
            details={"install": "https://direnv.net/docs/installation.html"})
    state = direnv_status(direnv, directory, environ, log)
    if not state["exists"]:
        raise ScaffoldError(
            "ENVIRONMENT_REQUIRED", "The environment file %s does not exist." % state["path"],
            details={"file": state["path"]},
            repairs=[repair(["bdev", "env", "init", "--checkout", str(identity.core)])])
    if state["allowed"] is not True:
        message = state.get("error") or "The environment file %s is not approved for its current contents." % state["path"]
        raise ScaffoldError(
            "ENVIRONMENT_UNAPPROVED", message, details={"file": state["path"]},
            repairs=[repair(["direnv", "allow", str(directory)], requires_user_action=True,
                            note="Inspect %s first, then run this yourself." % state["path"])])
    return direnv, directory


def load_environment(identity, environ, log=None, with_pythonpath=False):
    """Load the mapped environment into a private child and return its variables.

    Evaluates the approved `.envrc` (user-reviewed code) with direnv, regardless
    of any interactive hook, then validates the result against the frozen
    checkout identity.
    """
    direnv, directory = require_environment(identity, environ, log)
    require_depot_tools(identity)
    base = clean_environ(environ)
    with tempfile.TemporaryDirectory(prefix="scaffold-env-") as scratch:
        target = Path(scratch) / "environment.json"
        command = [direnv, "exec", str(directory), sys.executable, "-I", str(ENVIRONMENT_DUMP), str(target)]
        result = run_capture(command, str(directory), base, log, timeout=120)
        if result.returncode != 0 or not target.is_file():
            raise ScaffoldError(
                "ENVIRONMENT_LOAD_FAILED",
                "The approved environment %s failed to load." % envrc_path(directory),
                details={"exit_code": result.returncode, "stderr": result.stderr.strip()[-2000:]},
                repairs=[repair(["direnv", "exec", str(directory), "true"], requires_user_action=False,
                                note="Reproduces the load to inspect the failure.")])
        loaded = json.loads(target.read_text(encoding="utf-8"))["environ"]
    check_identity(identity, loaded, with_pythonpath)
    loaded["BRAVE_LAUNCHER_CHECKOUT_DIR"] = str(identity.core)
    return loaded


def check_identity(identity, loaded, with_pythonpath=False):
    """Raise CHECKOUT_ENV_CONFLICT unless the environment names this checkout."""
    depot = require_depot_tools(identity)
    expected = {
        "BRAVE_BROWSER_DIR": identity.outer,
        "BRAVE_SRC_ROOT": identity.src,
        "BRAVE_CORE_DIR": identity.core,
        "BRAVE_DEPOT_TOOLS_DIR": depot,
        "VPYTHON3": depot / "vpython3",
    }
    problems = []
    for name, want in expected.items():
        got = loaded.get(name)
        if not got:
            problems.append({"variable": name, "expected": str(want), "actual": None})
        elif os.path.realpath(got) != os.path.realpath(str(want)):
            problems.append({"variable": name, "expected": str(want), "actual": got})
    launcher = loaded.get("BRAVE_LAUNCHER_CHECKOUT_DIR")
    if launcher and os.path.realpath(launcher) != os.path.realpath(str(identity.core)):
        problems.append({"variable": "BRAVE_LAUNCHER_CHECKOUT_DIR", "expected": str(identity.core),
                         "actual": launcher})
    if problems:
        raise ScaffoldError(
            "CHECKOUT_ENV_CONFLICT",
            "The loaded environment does not select the requested checkout.",
            details={"mismatches": problems},
            repairs=[repair(["bdev", "env", "init", "--checkout", str(identity.core)],
                            note="Regenerates the scaffold-owned environment; review before approving.")])
    return problems
