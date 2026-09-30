# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Drift inspection and patch update."""

from __future__ import annotations

from ..common.procs import run_capture
from ..common.results import Result, ScaffoldError
from . import packages, patches
from .cmd_build import prepare
from .records import track


def cmd_drift(ctx):
    identity = ctx.identity()
    report = patches.collect_drift(identity)
    if ctx.parsed.get("diff") or report.files:
        patches.add_numstats(identity, report, ctx.log)
    files = [entry.to_dict() for _, entry in sorted(report.files.items())]
    result = Result(command="drift", data={"drifted": files, "metadata_complete": report.complete,
                                           "incomplete_reasons": report.incomplete,
                                           "patchinfo_files": report.patchinfo_count})
    lines = ["Checked %d patch metadata file(s); %d drifted file(s)." % (report.patchinfo_count, len(files))]
    for entry, drifted in zip(files, (item for _, item in sorted(report.files.items()))):
        lines.append("  %s (%s) %s" % (entry["path"], ", ".join(entry["reasons"]), entry["numstat"]))
        if ctx.parsed.get("diff"):
            repository = drifted.repository or identity.src
            diff = run_capture(["git", "-C", str(repository), "diff", "--", drifted.relative or entry["path"]],
                               str(repository), None, ctx.log, timeout=120)
            lines.extend("      " + line for line in (diff.stdout.splitlines() or ["(no git diff)"]))
    if not report.complete:
        lines.append("Evidence is incomplete, so this is not a clean result:")
        lines.extend("  - " + reason for reason in report.incomplete)
        result.add_warning("INCOMPLETE_EVIDENCE", "; ".join(report.incomplete))
    elif not files:
        lines.append("All patched files match their metadata.")
    result.text = "\n".join(lines)
    return result


def cmd_patches_update(ctx):
    identity = ctx.identity()
    execution = prepare(ctx, identity, "mac")
    with track(ctx, "patches update", identity, {}, validated=True) as op:
        argv, code = packages.run(ctx, execution, ["run", "update_patches", *ctx.parsed.forwarded])
        if code != 0:
            raise ScaffoldError("CHILD_FAILED", "update_patches exited with status %d." % code,
                                details={"argv": argv}, child_exit_code=code)
        status = run_capture(["git", "-C", str(identity.core), "status", "--short", "--branch"], str(identity.core),
                             None, ctx.log, timeout=120)
        changes = [line for line in status.stdout.splitlines()[1:]]
        result = Result(command="patches update", child_exit_code=0, data={"argv": argv, "changed_files": changes})
        result.text = ("Patch changes for review (nothing was committed):\n  " + "\n  ".join(changes)) if changes \
            else "No patch changes detected."
        return op.complete(result)
