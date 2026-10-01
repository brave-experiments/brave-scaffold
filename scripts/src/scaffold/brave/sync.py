# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The pieces of a source sync that do not depend on how a command is assembled."""

from __future__ import annotations

import ast
from pathlib import Path

from ..common import tools as tools_module
from ..common.results import ScaffoldError, repair
from . import android_deps, freshness, packages, patches, steps as step_module, sync_scope, sync_model


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


def sync_arguments(ctx, target, forwarded, identity=None):
    arguments = ["run", "sync"]
    if target == "android":
        identity = identity or ctx.identity()
        existing = gclient_targets(identity)
        if existing is None:
            raise ScaffoldError("PREPARATION_CONFLICT", "The checkout's .gclient is missing or unreadable.",
                                details={"file": str(identity.workspace / ".gclient")})
        arguments.append("--target_os=" + ",".join(target_os_union(existing, ["android"])))
    return [*arguments, *forwarded]


def reject_blanket_adoption(ctx):
    if ctx.parsed.get("adopt_local_changes"):
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Blanket adoption is disabled: it cannot prove which local changes a sync may discard. "
                            "No approval baseline was saved and no sync was started.")


def local_work_conflicts(ctx, identity, model):
    """Evidence of local work that a source sync could reset or overwrite.

    Covers every repository the sync can reset, and the files applying patches afterwards would replace.
    Changes that are the recorded output of patch or support preparation are not local work.
    """
    generated = {str(path.relative_to(identity.src)): patches.sha256_or_none(path)
                 for path in model.generated}
    plan = patches.plan_patch_preparation(identity, ctx.log, ctx.state_root, extra_expected=generated)
    report = plan.report
    expected = {identity.src / path for path, digest in report.patched_paths.items()
                if digest and patches.sha256_or_none(identity.src / path) == digest}
    expected |= android_deps.recorded_results(identity, ctx.state_root)
    expected |= patches.core_written_paths(identity, ctx.state_root)
    expected |= model.generated
    model.writes.update({identity.src / path: "Core patch preparation or version update"
                         for path in set(plan.writes) | set(patches.CORE_WRITTEN)})
    metadata = set(plan.metadata_writes)
    for entry in report.inventory.entries if report.inventory is not None else []:
        if entry.has_patch and entry.repository.path in model.resets:
            metadata.add(str(entry.info))
    model.writes.update({Path(path): "Core patch metadata write" for path in metadata})
    scope = sync_scope.sync_repositories(identity)
    scope.repositories = list(dict.fromkeys([*scope.repositories, *model.repositories]))
    conflicts = sync_scope.local_work(identity, scope, expected,
                                      sync_scope.read_baseline(identity, ctx.state_root), ctx.log,
                                      reset_repositories=model.resets, writes=model.writes, unknown_writes=model.unknown,
                                      reset_upstream=model.chromium != "skipped")
    # Sync always calls version.py update, even when every patch is current.
    version_conflicts, _ = patches.write_set_conflicts(identity, [], {},
        {str(path.relative_to(identity.src)): patches.sha256_or_none(path) for path in expected
         if path.is_relative_to(identity.src)}, ctx.log)
    conflicts.extend(version_conflicts)
    if model.blocked:
        conflicts.extend({"path": str(identity.workspace / ".gclient"), "reason": reason}
                         for reason in model.unknown)
    if plan.action == "conflict":
        conflicts.extend(plan.conflicts)
    return conflicts


def reject_deletion_options(identity, forwarded):
    deletion = [token for token in forwarded
                if token.split("=", 1)[0] in ("-D", "--delete_unused_deps", "--delete_unversioned_trees")]
    if deletion:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "The forwarded sync options may delete repositories or untracked work, and their "
                            "complete deletion scope cannot be established; nothing was changed.",
                            details={"options": deletion},
                            repairs=[repair(["bdev", "sync", "--checkout", str(identity.core)],
                                            note="Repeat without dependency-deletion options.")])


def do_sync_phase(ctx, execution, op, target, forwarded):
    identity = execution.identity
    reject_deletion_options(identity, forwarded)
    arguments = sync_arguments(ctx, target, forwarded, identity)
    model = sync_model.inspect(identity, execution.toolchain, arguments, execution.environ, ctx.log)
    conflicts = local_work_conflicts(ctx, identity, model)
    if conflicts:
        raise ScaffoldError("PREPARATION_CONFLICT",
                            "Sync could overwrite local work in %d place(s); nothing was changed." % len(conflicts),
                            details={"files": conflicts[:50], "total": len(conflicts), "scope": model.detail()},
                            repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)])])
    before_files = sync_scope.snapshot(identity, sync_scope.sync_repositories(identity), ctx.log, include_core=True)
    before = {"core_head": freshness.resolve_head(identity.core, ctx.log),
              "chromium_head": freshness.resolve_head(identity.src, ctx.log)}
    op.start("sync", arguments=arguments, before=before, sync_scope=model.detail(),
            **step_module.sync_step(identity, arguments, tools_module.package_argv(execution.toolchain, arguments), model=model).record())
    argv, code = packages.run(ctx, execution, arguments)
    if code != 0:
        op.fail("sync", exit=code)
        raise ScaffoldError("CHILD_FAILED", "The sync command exited with status %d." % code,
                            details={"argv": argv, "phase": "sync"}, child_exit_code=code)
    after = {"core_head": freshness.resolve_head(identity.core, ctx.log),
             "chromium_head": freshness.resolve_head(identity.src, ctx.log)}
    op.succeed("sync", exit=0, revisions_before=before, revisions_after=after)
    try:
        sync_scope.checkpoint(identity, ctx.state_root, ctx.log, before=before_files)
    except ScaffoldError as error:
        op.note("sync-checkpoint", outcome="not recorded", reason=error.message)
    return {"argv": argv, "revisions_before": before, "revisions_after": after, "scope": model.detail()}


def plan_step(ctx, identity, arguments, toolchain, needs=()):
    """Read-only scope and preservation checks, repeated with the approved environment at execution."""
    model = sync_model.inspect(identity, toolchain, arguments, ctx.environ, ctx.log)
    try:
        conflicts = local_work_conflicts(ctx, identity, model)
    except ScaffoldError as error:
        conflicts = [{"path": error.details.get("repository", str(identity.core)), "reason": error.message}]
    return step_module.sync_step(identity, arguments,
        tools_module.package_argv(toolchain, arguments) if toolchain else None, needs, model=model, conflicts=conflicts)
