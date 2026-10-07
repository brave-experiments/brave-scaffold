# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkout status, with operation history kept separate from source verification."""

import re
import shutil
from datetime import datetime

from ..common.procs import run_capture
from ..common.results import Result, ScaffoldError
from . import records


def git(ctx, core, *args, optional=False):
    result = run_capture(["git", "--no-optional-locks", "-C", str(core), *args], str(core), None,
                         ctx.log, timeout=30)
    if result.returncode or result.truncated or result.timed_out:
        if optional and not result.truncated and not result.timed_out:
            return None
        raise ScaffoldError("READINESS_INCOMPLETE", "Could not read complete Git status; no files were changed.")
    return result.stdout


DIFF_BASE = "master"


def diff_stat(ctx, core):
    """The branch's committed changes against master, from `git diff --shortstat master...HEAD`.

    None when Git cannot compare (for example there is no local master branch).
    """
    text = git(ctx, core, "diff", "--shortstat", DIFF_BASE + "...HEAD", optional=True)
    if text is None:
        return None
    counts = {"files": 0, "insertions": 0, "deletions": 0}
    for number, word in re.findall(r"(\d+) (file|insertion|deletion)", text):
        counts[{"file": "files", "insertion": "insertions", "deletion": "deletions"}[word]] = int(number)
    return {"base": DIFF_BASE, **counts, "summary": text.strip() or None}


def changes_from_porcelain(text):
    changes = []
    entries = iter(text.split("\0"))
    for entry in entries:
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        item = {"path": path, "status": code, "staged": code[0] not in " ?",
                "unstaged": code[1] not in " ?", "untracked": code == "??"}
        if "R" in code or "C" in code:
            item["original_path"] = next(entries, "")
        changes.append(item)
    return changes


def history_entry(record, head):
    details = record.get("details") or {}
    effective = details.get("effective") or {}
    source = record.get("source") or {}
    revision = source.get("core_head")
    return {"operation_id": record.get("operation_id"), "command": record.get("command"),
            "target": effective.get("target", details.get("target")), "suite": details.get("suite"),
            "status": record.get("status"), "finished": record.get("finished"),
            "branch": source.get("core_branch"), "revision": revision, "same_head": revision == head if revision else None,
            "output_dir": effective.get("output_dir"), "device": details.get("device"),
            "log": (record.get("logs") or {}).get("diagnostic"),
            "artifacts": [a.get("path") for a in record.get("artifacts", [])],
            "verification": "unknown"}


def finished_at(entry):
    try:
        return datetime.fromisoformat(entry["finished"]).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def run_status(ctx):
    identity = ctx.identity()
    core = identity.core
    head = git(ctx, core, "rev-parse", "HEAD").strip()
    branch = git(ctx, core, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True)
    branch = branch.strip() if branch else None
    all_branches = bool(ctx.parsed.get("all_branches"))
    upstream = git(ctx, core, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", optional=True)
    changes = changes_from_porcelain(git(ctx, core, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    counts = {kind: sum(c[kind] for c in changes) for kind in ("staged", "unstaged", "untracked")}
    comparison = git(ctx, core, "rev-list", "--left-right", "--count", "origin/master...HEAD", optional=True)
    base = None
    if comparison:
        behind, ahead = map(int, comparison.split())
        base = {"ref": "origin/master", "ahead": ahead, "behind": behind}
    stat = diff_stat(ctx, core)
    operations = list(records.all_operations(ctx.state_root, core))
    latest = {}
    incomplete = []
    for record in operations:
        recorded_branch = (record.get("source") or {}).get("core_branch")
        if not all_branches and (not branch or recorded_branch != branch):
            continue
        if record.get("state") != "complete":
            incomplete.append({"operation_id": record.get("operation_id"), "command": record.get("command"),
                               "branch": recorded_branch, "started": record.get("started"), "process_state": "unknown"})
            continue
        if record.get("command") not in ("build", "build-run", "sync-build", "sync-build-run", "test"):
            continue
        entry = history_entry(record, head)
        kind = "test" if entry["command"] == "test" else "build"
        key = (entry["branch"], kind, entry["target"], entry["suite"] if kind == "test" else None)
        if key not in latest or finished_at(entry) >= finished_at(latest[key]):
            latest[key] = entry
    history = sorted(latest.values(), key=lambda e: (e["command"] == "test", e["target"] or "", e["suite"] or ""))
    outputs = [{"path": s.get("output_dir"), "needs_revalidation": s.get("needs_revalidation", False)}
               for s in records.output_states(identity, ctx.state_root)]
    damaged = records.damaged_output_records(identity, ctx.state_root)
    try:
        free = shutil.disk_usage(core).free
    except OSError:
        free = None
    data = {"branch": branch, "head": head, "history_scope": "all-branches" if all_branches else "current-branch",
            "upstream": upstream.strip() if upstream else None, "base": base,
            "changes": changes, "change_counts": counts, "diff_stat": stat, "history": history,
            "current_test_verification": "unknown", "outputs": outputs, "damaged_output_records": damaged,
            "incomplete_operations": incomplete,
            "disk_free_bytes": free}
    lines = ["%s · %s" % (identity.alias or "Checkout", data["branch"] or "detached HEAD"), str(core),
             "", "Git", "  HEAD       " + head[:12], "  Upstream   " + (data["upstream"] or "Not configured")]
    lines.append("  Base       %d behind origin/master; %d ahead" % (base["behind"], base["ahead"])
                 if base else "  Base       origin/master comparison unavailable")
    lines.append("  Diff       %s...HEAD: %s" % (DIFF_BASE, stat["summary"] or "no changes") if stat
                 else "  Diff       %s...HEAD unavailable (no %s branch to compare with)" % (DIFF_BASE, DIFF_BASE))
    lines.append("  Changes    %(staged)d staged · %(unstaged)d unstaged · %(untracked)d untracked" % counts)
    lines += ["    %s %s" % (c["status"], repr(c["path"])) for c in changes[:10]]
    if len(changes) > 10:
        lines.append("    … %d more (use --json for all paths)" % (len(changes) - 10))
    lines += ["", "Tests for current checkout", "  ⚪ Current source state not verified by saved test records.",
              "", "Previous operations (history, not current verification)"]
    for entry in history:
        relation = "same HEAD; file contents not verified" if entry["same_head"] else (
            "different HEAD" if entry["same_head"] is False else "revision unknown")
        lines.append("  %s · %s · %s · %s" % (entry["target"] or "unknown target",
                     entry["suite"] or entry["command"], {"ok": "succeeded", "error": "failed"}.get(entry["status"], entry["status"] or "unknown"), entry["finished"] or "time unknown"))
        lines.append("    Branch: " + (entry["branch"] or "branch unknown"))
        lines.append("    %s · %s" % ((entry["revision"] or "unknown")[:12], relation))
        if entry["output_dir"]:
            lines.append("    Output: " + entry["output_dir"])
        if entry["device"]:
            lines.append("    Device: " + entry["device"])
        if entry["log"]:
            lines.append("    Log: " + entry["log"])
    if not latest:
        lines.append("  No recorded builds or tests." if all_branches else "  No recorded builds or tests for this branch.")
    if not all_branches:
        lines.append("  Use --all-branches to include other branches and older records with unknown branches.")
    attention = ["  ⚠️ Output needs revalidation: %s" % o["path"] for o in outputs if o["needs_revalidation"]]
    attention += ["  ⚠️ Output history is unreadable: %s (the next build or test of that output sets it aside "
                  "and marks it for revalidation)" % path for path in damaged]
    attention += ["  ⚠️ Unfinished %s record %s (%s); process state unknown" % (
        r["command"], r["operation_id"], str(r["started"]) + "; branch: " + (r["branch"] or "unknown")) for r in incomplete]
    if attention:
        lines += ["", "Needs attention", *attention]
    if free is not None and free < 200_000_000_000:
        lines += ["", "Disk", "  %.1f GB free" % (free / 1e9)]
    lines += ["", "Saved timestamps include their UTC offset. No build freshness or process checks were run."]
    return Result(command="status", data=data, text="\n".join(lines))
