# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkout status, with operation history kept separate from source verification."""

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
            "revision": revision, "same_head": revision == head if revision else None,
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
    upstream = git(ctx, core, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", optional=True)
    changes = changes_from_porcelain(git(ctx, core, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    counts = {kind: sum(c[kind] for c in changes) for kind in ("staged", "unstaged", "untracked")}
    comparison = git(ctx, core, "rev-list", "--left-right", "--count", "origin/master...HEAD", optional=True)
    base = None
    if comparison:
        behind, ahead = map(int, comparison.split())
        base = {"ref": "origin/master", "ahead": ahead, "behind": behind}
    operations = list(records.all_operations(ctx.state_root, core))
    latest = {}
    incomplete = []
    for record in operations:
        if record.get("state") != "complete":
            incomplete.append({"operation_id": record.get("operation_id"), "command": record.get("command"),
                               "started": record.get("started"), "process_state": "unknown"})
            continue
        if record.get("command") not in ("build", "build-run", "sync-build", "sync-build-run", "test"):
            continue
        entry = history_entry(record, head)
        kind = "test" if entry["command"] == "test" else "build"
        key = (kind, entry["target"], entry["suite"] if kind == "test" else None)
        if key not in latest or finished_at(entry) >= finished_at(latest[key]):
            latest[key] = entry
    history = sorted(latest.values(), key=lambda e: (e["command"] == "test", e["target"] or "", e["suite"] or ""))
    outputs = [{"path": s.get("output_dir"), "needs_revalidation": s.get("needs_revalidation", False)}
               for s in records.output_states(identity, ctx.state_root)]
    try:
        free = shutil.disk_usage(core).free
    except OSError:
        free = None
    data = {"branch": branch.strip() if branch else None, "head": head,
            "upstream": upstream.strip() if upstream else None, "base": base,
            "changes": changes, "change_counts": counts, "history": history,
            "current_test_verification": "unknown", "outputs": outputs, "incomplete_operations": incomplete,
            "disk_free_bytes": free}
    lines = ["%s · %s" % (identity.alias or "Checkout", data["branch"] or "detached HEAD"), str(core),
             "", "Git", "  HEAD       " + head[:12], "  Upstream   " + (data["upstream"] or "Not configured")]
    lines.append("  Base       %d behind origin/master; %d ahead" % (base["behind"], base["ahead"])
                 if base else "  Base       origin/master comparison unavailable")
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
        lines.append("    %s · %s" % ((entry["revision"] or "unknown")[:12], relation))
        if entry["output_dir"]:
            lines.append("    Output: " + entry["output_dir"])
        if entry["device"]:
            lines.append("    Device: " + entry["device"])
        if entry["log"]:
            lines.append("    Log: " + entry["log"])
    if not latest:
        lines.append("  No recorded builds or tests.")
    attention = ["  ⚠️ Output needs revalidation: %s" % o["path"] for o in outputs if o["needs_revalidation"]]
    attention += ["  ⚠️ Unfinished %s record %s (%s); process state unknown" % (
        r["command"], r["operation_id"], r["started"]) for r in incomplete]
    if attention:
        lines += ["", "Needs attention", *attention]
    lines += ["", "Disk", "  %.1f GB free" % (free / 1e9) if free is not None else "  Free space unavailable",
              "", "Saved timestamps include their UTC offset. No build freshness or process checks were run."]
    return Result(command="status", data=data, text="\n".join(lines))
