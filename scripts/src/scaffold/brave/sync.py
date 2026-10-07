# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The pieces of a source sync that do not depend on how a command is assembled."""

from __future__ import annotations

import ast

from ..common import tools as tools_module
from ..common.results import ScaffoldError
from . import freshness, ios, packages, steps as step_module


def gclient_targets(identity):
    """Existing target_os values from the checkout's .gclient (literals only; nothing is executed)."""
    path = identity.workspace / ".gclient"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "target_os" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
            found = [value] if isinstance(value, str) else list(value)
    return found


def target_os_union(existing, requested):
    unrelated = [item for item in existing if item not in ("android", "ios", "mac", "macos", "desktop")]
    mobile = {item for item in existing if item in ("android", "ios")} | set(requested)
    return unrelated + [item for item in ("android", "ios") if item in mobile]


def mobile_targets(target):
    """The mobile targets (android, ios) named by a target string or an iterable of them."""
    named = [target] if isinstance(target, str) else list(target)
    return [item for item in ("android", "ios") if item in named]


def sync_arguments(ctx, target, forwarded, identity=None):
    arguments = ["run", "sync"]
    requested = mobile_targets(target)
    if requested:
        if any(item == "--target_os" or item.startswith("--target_os=") for item in forwarded):
            raise ScaffoldError("SELECTOR_CONFLICT", "Mobile sync targets build --target_os from the checkout's "
                                "existing targets; remove --target_os from the sync arguments.")
        if "ios" in requested and "--nohooks" in forwarded:
            raise ScaffoldError("SELECTOR_CONFLICT", "Syncing iOS needs Core's hooks, which bootstrap the iOS "
                                "project; remove --nohooks.")
        identity = identity or ctx.identity()
        existing = gclient_targets(identity)
        if existing is None:
            raise ScaffoldError("PREPARATION_CONFLICT", "The checkout's .gclient is missing or unreadable.",
                                details={"file": str(identity.workspace / ".gclient")})
        arguments.append("--target_os=" + ",".join(target_os_union(existing, requested)))
    return [*arguments, *forwarded]


def do_sync_phase(ctx, execution, op, target, forwarded):
    """Run Core's sync command with its normal source and dependency effects."""
    identity = execution.identity
    arguments = sync_arguments(ctx, target, forwarded, identity)
    before = {"core_head": freshness.resolve_head(identity.core, ctx.log),
              "chromium_head": freshness.resolve_head(identity.src, ctx.log)}
    op.start("sync", arguments=arguments, before=before,
             **step_module.sync_step(identity, arguments, tools_module.package_argv(execution.toolchain, arguments)).record())
    argv, code = packages.run(ctx, execution, arguments)
    if code != 0:
        op.fail("sync", exit=code)
        raise ScaffoldError("CHILD_FAILED", "The sync command exited with status %d." % code,
                            details={"argv": argv, "phase": "sync"}, child_exit_code=code)
    after = {"core_head": freshness.resolve_head(identity.core, ctx.log),
             "chromium_head": freshness.resolve_head(identity.src, ctx.log)}
    if "ios" in mobile_targets(target):
        missing = ios.missing_bootstrap_artifacts(identity)
        if missing:
            op.fail("sync", missing_bootstrap=[str(path) for path in missing])
            raise ScaffoldError("PREPARATION_CONFLICT", "The sync finished but Core's iOS bootstrap files are "
                                "missing (%d, for example %s)." % (len(missing), missing[0]),
                                details={"missing": [str(path) for path in missing], "phase": "sync"},
                                repairs=[ios.bootstrap_repair(identity)])
    op.succeed("sync", exit=0, revisions_before=before, revisions_after=after)
    return {"argv": argv, "revisions_before": before, "revisions_after": after}


def plan_step(identity, arguments, toolchain, needs=()):
    """Describe dispatch; Core decides which repositories, patches, and hooks to update."""
    return step_module.sync_step(identity, arguments,
        tools_module.package_argv(toolchain, arguments) if toolchain else None, needs)
