# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkout status, with operation history kept separate from source verification."""

import json
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
    """Count committed branch changes from its common ancestor with the base."""
    ancestor = git(ctx, core, "merge-base", "refs/remotes/origin/master", "HEAD", optional=True)
    if ancestor is None:
        ancestor = git(ctx, core, "merge-base", DIFF_BASE, "HEAD", optional=True)
    if not ancestor:
        return None
    ancestor = ancestor.strip()
    text = git(ctx, core, "diff", "--shortstat", ancestor, "HEAD", optional=True)
    if text is None:
        return None
    counts = {"files": 0, "insertions": 0, "deletions": 0}
    for number, word in re.findall(r"(\d+) (file|insertion|deletion)", text):
        counts[{"file": "files", "insertion": "insertions", "deletion": "deletions"}[word]] = int(number)
    return {"base": ancestor, **counts, "summary": text.strip() or None}


def chromium_comparison(ctx, core, head):
    """Compare committed Chromium pins with the locally available remote base."""
    versions = []
    for ref in (head, "refs/remotes/origin/master"):
        try:
            text = git(ctx, core, "show", ref + ":package.json", optional=True)
            version = json.loads(text)["config"]["projects"]["chrome"]["tag"] if text else None
        except (ScaffoldError, ValueError, KeyError, TypeError):
            version = None
        versions.append(version if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", version) else None)
    current, base = versions
    status = "unavailable"
    if current and base:
        current_parts = tuple(map(int, current.split(".")))
        base_parts = tuple(map(int, base.split(".")))
        status = ("major_behind" if current_parts[0] < base_parts[0] else
                  "version_behind" if current_parts < base_parts else
                  "current" if current_parts == base_parts else "ahead")
    return {"base_ref": "origin/master", "branch_version": current, "base_version": base, "status": status}


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
    chromium = chromium_comparison(ctx, core, head)
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
            "upstream": upstream.strip() if upstream else None, "base": base, "chromium": chromium,
            "changes": changes, "change_counts": counts, "diff_stat": stat, "history": history,
            "current_test_verification": "unknown", "outputs": outputs, "damaged_output_records": damaged,
            "incomplete_operations": incomplete,
            "disk_free_bytes": free}
    lines = ["%s · %s" % (identity.alias or "Checkout", data["branch"] or "detached HEAD"), str(core),
             "", "Git", "  HEAD       " + head[:12], "  Upstream   " + (data["upstream"] or "Not configured")]
    if ctx.parsed.get("show_origin"):
        lines.append("  Origin     %d behind origin/master; %d ahead" % (base["behind"], base["ahead"])
                     if base else "  Origin     origin/master comparison unavailable")
    lines.append("  Branch base " + stat["base"][:12] if stat else "  Branch base unavailable")
    lines.append("  Diff       %s..HEAD: %s" % (stat["base"][:12], stat["summary"] or "no changes") if stat
                 else "  Diff       Branch comparison unavailable (no common ancestor with origin/master or master)")
    if chromium["status"] == "unavailable":
        lines.append("  Chromium   Comparison with origin/master unavailable (missing or invalid committed pin).")
    else:
        lines.append("  Chromium   %s (origin/master: %s; local remote ref)" % (
            chromium["branch_version"], chromium["base_version"]))
    if chromium["status"] == "version_behind":
        lines.append("  Notice     Branch Chromium %s is behind origin/master Chromium %s." % (
            chromium["branch_version"], chromium["base_version"]))
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
    if chromium["status"] == "major_behind":
        attention.insert(0, "  ⚠️ Branch uses Chromium %s; origin/master uses Chromium %s. "
                         "The branch is a major Chromium version behind." % (
                             chromium["branch_version"], chromium["base_version"]))
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
