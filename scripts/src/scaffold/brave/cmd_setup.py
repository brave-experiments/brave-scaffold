# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Setup, checkout registration, context, and environment commands."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import sys
from pathlib import Path

from ..common import config as config_module
from ..common import env as env_module
from ..common import identity as identity_module
from ..common import tools as tools_module
from ..common.checks import PASS
from ..common.platforms import capability_table
from ..common.procs import run_capture, run_streaming
from ..common.results import Result, ScaffoldError

MINIMAL_CONFIG = "schema_version = 1\n\n[logging]\ncommands = true\n"
GENERATED_MARKER = env_module.GENERATED_MARKER


def _direnv_version(direnv, environ, log):
    if not direnv:
        return None
    result = run_capture([direnv, "version"], os.getcwd(), env_module.clean_environ(environ), log, timeout=15)
    return result.stdout.strip() if result.returncode == 0 else None


# --- setup ----------------------------------------------------------------------


def setup(ctx):
    root = ctx.scaffold_root
    config = ctx.config
    created = False
    if not config.exists:
        config_module.atomic_write(config.path, MINIMAL_CONFIG)
        created = True
    direnv = env_module.find_direnv(ctx.environ)
    facts = {
        "scaffold_root": str(root),
        "runtime": {"python": sys.version.split()[0], "executable": sys.executable,
                    "expected": str(root / "scripts" / ".venv" / "bin" / "python")},
        "direnv": {"path": direnv, "version": _direnv_version(direnv, ctx.environ, ctx.log)},
        "git": shutil.which("git", path=ctx.environ.get("PATH")),
        "configuration": {"file": str(config.path), "created": created},
    }
    steps = []
    if direnv is None:
        steps.append("Install direnv: https://direnv.net/docs/installation.html")
    steps.append("Register a checkout: bdev checkout add <name> <path-to-checkout>")
    steps.append("Generate its environment: bdev env init --checkout <name>")
    steps.append("Review the generated file, then approve it yourself: direnv allow <environment-dir>")
    steps.append("Check readiness: bdev doctor --checkout <name>")
    lines = ["Scaffold: %s" % root, "Runtime: Python %s (%s)" % (facts["runtime"]["python"], sys.executable),
             "direnv: %s" % (direnv or "not installed"), "git: %s" % (facts["git"] or "not found"),
             "Configuration: %s%s" % (config.path, " (created)" if created else ""),
             "", "Nothing inside Brave Core was changed.", "", "Next steps:"]
    lines.extend("  %d. %s" % (index, step) for index, step in enumerate(steps, 1))
    result = Result(command="setup", data={**facts, "next_steps": steps}, text="\n".join(lines))
    return result


# --- checkout add / list ----------------------------------------------------------


def checkout_add(ctx):
    name, target = ctx.parsed.positionals
    if not config_module.ALIAS_PATTERN.fullmatch(name) or identity_module.looks_like_path(name):
        raise ScaffoldError("INVALID_INPUT", "%r is not a valid alias." % name,
                            details={"rule": "letters, digits, - and _; must start with a letter or digit"})
    path = Path(os.path.expanduser(target))
    if not path.is_absolute():
        path = Path(ctx.cwd) / path
    found = identity_module.discover_core(path) if path.exists() else []
    if not found:
        raise ScaffoldError("CHECKOUT_NOT_FOUND", "No Brave Core checkout was found at or above %s." % path,
                            details={"path": str(path)})
    if len(found) > 1:
        raise identity_module.ambiguous(found, str(path))
    core = found[0]
    identity = identity_module.build_identity(core, ctx.config, "explicit_path", target)
    identity_module.validate_layout(identity)
    for record in ctx.config.checkouts:
        if record.alias == name and record.core_real != identity.core:
            raise ScaffoldError("CONFIG_INVALID", "The alias %r already names %s." % (name, record.core),
                                details={"file": str(ctx.config.path)})
    outcome = config_module.upsert_checkout(ctx.config.path, identity.core, alias=name)
    text = "Checkout %s %s: %s\nConfiguration: %s\nNo files inside the checkout were changed." % (
        name, outcome, identity.core, ctx.config.path)
    result = Result(command="checkout add", data={"alias": name, "core": str(identity.core),
                                                    "outcome": outcome, "configuration": str(ctx.config.path)}, text=text)
    return result


def _approval(direnv, directory, environ, log):
    if directory is None:
        return {"state": "no environment configured"}
    if not direnv:
        return {"state": "direnv not installed"}
    state = env_module.direnv_status(direnv, directory, environ, log)
    if not state["exists"]:
        return {"state": "environment file missing", "file": state["path"]}
    if state.get("allowed") is True:
        return {"state": "approved", "file": state["path"]}
    return {"state": "not approved", "file": state["path"], "detail": state.get("error")}


def checkout_list(ctx):
    direnv = env_module.find_direnv(ctx.environ)
    rows, lines = [], []
    for record in ctx.config.checkouts:
        problem = None
        identity = None
        if not record.core_real.is_dir():
            problem = "path does not exist"
        elif not identity_module.is_core(record.core_real):
            problem = "not a Brave Core directory"
        else:
            identity = identity_module.build_identity(record.core_real, ctx.config, "configured")
            worktrees = identity_module.find_linked_worktrees(identity.core, identity.src, identity.outer)
            if worktrees:
                problem = "uses Git linked worktrees (unsupported)"
        approval = _approval(direnv, record.direnv_dir, ctx.environ, ctx.log)
        rows.append({"alias": record.alias, "core": str(record.core_real),
                     "environment": str(record.direnv_dir) if record.direnv_dir else None,
                     "environment_state": approval["state"], "problem": problem})
        lines.append("%-12s %s\n%12s environment: %s%s" % (
            record.alias or "(no alias)", record.core_real, "", approval["state"],
            "\n%12s PROBLEM: %s" % ("", problem) if problem else ""))
    if not rows:
        lines.append("No checkouts are registered. Register one with: bdev checkout add <name> <path>")
    return Result(command="checkout list", data={"checkouts": rows, "configuration": str(ctx.config.path)},
                  text="\n".join(lines))


# --- context ----------------------------------------------------------------------


def _git_head(repo):
    head = Path(repo) / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("ref:"):
        return {"branch": text[4:].strip().removeprefix("refs/heads/")}
    return {"detached": text}


def context(ctx):
    identity = ctx.identity(required=False, validate=False)
    config = ctx.config
    result = Result(command="context")
    data = {"configuration": {"file": str(config.path), "exists": config.exists},
            "host_platform": sys.platform}
    lines = ["Configuration: %s%s" % (config.path, "" if config.exists else " (not found)")]
    if identity is None:
        lines.append("Checkout: none selected (the current directory is not inside a Brave checkout)")
        data["checkout"] = None
        result.data, result.text = data, "\n".join(lines)
        return result
    worktrees = identity_module.find_linked_worktrees(identity.core, identity.src, identity.outer)
    data["checkout"] = {"core": str(identity.core), "chromium_src": str(identity.src),
                        "workspace": str(identity.workspace), "outer": str(identity.outer),
                        "alias": identity.alias, "selection_source": identity.selection_source,
                        "head": _git_head(identity.core)}
    data["layout"] = {"supported": not worktrees, "linked_worktrees": worktrees}
    lines += ["Checkout: %s" % identity.core, "  Selected by: %s" % identity.selection_source,
              "  Alias: %s" % (identity.alias or "(none)"), "  Chromium src: %s" % identity.src]
    if worktrees:
        result.add_warning("UNSUPPORTED_CAPABILITY",
                           "This checkout uses Git linked worktrees, which are unsupported. "
                           "Select an existing full checkout.", worktrees=worktrees)
        lines.append("  Layout: UNSUPPORTED (Git linked worktrees)")
        result.data, result.text = data, "\n".join(lines)
        return result
    direnv = env_module.find_direnv(ctx.environ)
    record = identity.record
    approval = _approval(direnv, record.direnv_dir if record else None, ctx.environ, ctx.log)
    data["environment"] = {"directory": str(record.direnv_dir) if record and record.direnv_dir else None,
                           **approval}
    lines.append("  Environment: %s" % approval["state"])
    toolchain, checks = tools_module.inspect_toolchain(identity, ctx.log)
    data["tools"] = {"ready": toolchain is not None, "checks": [check.to_dict() for check in checks]}
    for check in checks:
        lines.append("  Tool %s: %s - %s" % (check.name, check.status, check.summary))
    result.checks = [check.to_dict() for check in checks]
    result.data, result.text = data, "\n".join(lines)
    return result


def capabilities(ctx):
    rows = capability_table()
    lines = ["%-16s %-8s %-15s %-8s %-6s %s" % ("host", "target", "operation", "config", "arch", "status")]
    for row in rows:
        lines.append("%-16s %-8s %-15s %-8s %-6s %s%s" % (
            row["host"], row["target"], row["operation"], row["configuration"], row["architecture"],
            row["status"], "  (%s)" % row["note"] if row["note"] else ""))
    return Result(command="capabilities", data={"capabilities": rows}, text="\n".join(lines))


# --- environments -------------------------------------------------------------------


def _environment_dir(ctx, identity):
    """Directory for the mapped environment; returns (path, configured_text)."""
    record = identity.record
    if record and record.direnv_dir:
        return record.direnv_dir, record.direnv_dir_text
    name = identity.alias or "checkout-" + hashlib.sha256(str(identity.core).encode()).hexdigest()[:8]
    text = "environments/%s" % name
    return Path(os.path.normpath(ctx.config.directory / text)), text


def env_init(ctx):
    identity = ctx.identity()
    directory, text = _environment_dir(ctx, identity)
    real_dir = Path(os.path.realpath(directory))
    for protected in (identity.core, identity.src, identity.outer):
        if real_dir == protected or protected in real_dir.parents:
            raise ScaffoldError(
                "INVALID_INPUT", "The environment directory %s is inside the checkout; "
                "scaffold environments must stay outside Brave Core." % directory,
                details={"directory": str(directory)})
    content = env_module.envrc_text(identity, ctx.config.path)
    target = env_module.envrc_path(directory)
    status = "created"
    if target.is_symlink():
        status = "preserved"
    elif target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing == content:
            status = "unchanged"
        elif GENERATED_MARKER in existing:
            status = "updated"
        else:
            status = "preserved"
    if status in ("created", "updated"):
        directory.mkdir(parents=True, exist_ok=True)
        config_module.atomic_write(target, content)
    config_module.upsert_checkout(ctx.config.path, identity.core, direnv_dir=text)
    lines = ["Environment file %s: %s" % (status, target)]
    if status == "preserved":
        lines += ["This file was not generated by the scaffold, so it was left alone.",
                  "To use the scaffold environment, integrate this content yourself:", "", content]
    else:
        lines += ["", content]
    lines += ["This file runs: %s" % (env_module.scaffold_root() / "scripts" / "bdev"),
              "with configuration: %s" % ctx.config.path, "",
              "Nothing inside Brave Core was changed and nothing was approved.",
              "Review the file, then approve it yourself:", "  direnv allow %s" % shlex.quote(os.path.realpath(directory))]
    result = Result(command="env init", data={"file": str(target), "status": status, "content": content,
                                               "approval_command": ["direnv", "allow", os.path.realpath(directory)]},
                    text="\n".join(lines))
    if status == "updated":
        result.add_warning("ENVIRONMENT_UNAPPROVED", "The file changed, so its earlier approval no longer applies.")
    return result


def env_export(ctx):
    identity = ctx.identity()
    with_pythonpath = bool(ctx.parsed.get("with_pythonpath"))
    if ctx.parsed.get("format") not in (None, "bash", "json"):
        raise ScaffoldError("INVALID_INPUT", "env export supports --format bash.")
    derived = env_module.derive_env(identity, ctx.environ, with_pythonpath)
    text = env_module.format_bash(derived, ctx.environ, with_pythonpath)
    return Result(command="env export", data={"format": "bash", "exports": derived.exports, "text": text}, text=text)


def env_check(ctx):
    identity = ctx.identity()
    loaded = env_module.load_environment(identity, ctx.environ, ctx.log,
                                         with_pythonpath=bool(ctx.parsed.get("with_pythonpath")))
    derived = env_module.derive_env(identity, {}, False)
    rows = []
    for name in ("BRAVE_BROWSER_DIR", "BRAVE_SRC_ROOT", "BRAVE_CORE_DIR", "BRAVE_DEPOT_TOOLS_DIR", "VPYTHON3"):
        rows.append({"variable": name, "expected": derived.exports[name], "observed": loaded.get(name), "ok": True})
    toolchain, checks = tools_module.inspect_toolchain(identity, ctx.log)
    lines = ["Environment %s evaluated (approved) and matches %s" % (
        env_module.envrc_path(identity.direnv_dir), identity.core)]
    lines += ["  OK   %s" % row["variable"] for row in rows]
    lines += ["  %s %s: %s" % ("OK  " if check.status == PASS else check.status.upper(), check.name, check.summary)
              for check in checks]
    result = Result(command="env check", data={"variables": rows, "tools": toolchain.describe() if toolchain else None},
                    checks=[check.to_dict() for check in checks], text="\n".join(lines))
    return result


def shell(ctx):
    identity = ctx.identity()
    loaded = env_module.load_environment(identity, ctx.environ, ctx.log)
    program = ctx.environ.get("SHELL") or "/bin/zsh"
    code = run_streaming([program], str(identity.core), loaded, ctx.log, json_mode=ctx.json_mode)
    result = Result(command="shell", data={"shell": program, "cwd": str(identity.core), "exit": code},
                    child_exit_code=code)
    return result
