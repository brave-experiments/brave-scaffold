# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Operation records and per-output build state, kept outside Brave Core."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from ..common.config import atomic_write, scaffold_root
from ..common.procs import run_capture
from ..common.redaction import redact_report
from ..common.results import Cancelled, ScaffoldError
from . import freshness

CLI_VERSION = "0.1.0"
KEEP_OPERATIONS = 100


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def store_root(root=None):
    return Path(root or scaffold_root()) / ".bdev"


def checkout_key(core):
    return hashlib.sha256(os.path.realpath(core).encode()).hexdigest()[:16]


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path, data):
    atomic_write(Path(path), json.dumps(redact_report(data), indent=2, sort_keys=True) + "\n")


class Operation:
    """A durable record written before mutation and completed afterward.

    An operation that dies without finishing keeps `state: "incomplete"`, so a later inspection can report
    the interruption instead of claiming success. Every dispatched command is saved before it starts.
    """

    def __init__(self, command, identity, details, root=None, evidence=None):
        self.root = store_root(root)
        self.id = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        self.path = self.root / "operations" / (self.id + ".json")
        self.data = {
            "operation_id": self.id, "cli_version": CLI_VERSION, "command": command, "state": "incomplete",
            "started": now(), "finished": None, "checkout": str(identity.core),
            "chromium_src": str(identity.src), "details": details, "steps": [], "commands": [],
            "artifacts": [], "status": None, "exit_code": None, "child_exit_code": None, "error": None,
            "cleanup": None, **(evidence or {}),
        }
        self.save()

    def save(self):
        _write(self.path, self.data)

    def step(self, name, **fields):
        self.data["steps"].append({"name": name, "at": now(), **fields})
        self.save()

    def update(self, **fields):
        self.data.update(fields)
        self.save()

    def detail(self, **fields):
        self.data["details"].update(fields)
        self.save()

    def command_dispatched(self, entry):
        self.data["commands"].append(entry)
        self.save()

    def finish(self, status, exit_code, commands=None, artifacts=None, child_exit_code=None, error=None, cleanup=None):
        self.data.update(state="complete", finished=now(), status=status, exit_code=exit_code)
        for name, value in (("commands", commands), ("artifacts", artifacts), ("child_exit_code", child_exit_code),
                            ("error", error), ("cleanup", cleanup)):
            if value is not None:
                self.data[name] = value
        self.save()
        prune(self.root)

    def complete(self, result):
        """Finish from a command's result and stamp the operation ID on it."""
        result.operation_id = self.id
        self.finish(result.status, result.exit_code, artifacts=[dict(item) for item in result.artifacts],
                    child_exit_code=result.child_exit_code, error=result.error and
                    {"code": result.error["code"], "message": result.error["message"]})
        return result


def describe_start(ctx, identity, validated=False):
    """Evidence stored with every operation: environment identity (no values), source state, log destinations."""
    evidence = {"logs": {"commands": "stderr" if ctx.log.enabled else "disabled",
                         "child_output": "stderr" if ctx.json_mode else "terminal"}}
    record = identity.record
    if record is not None and record.direnv_dir is not None:
        envrc = Path(os.path.realpath(record.direnv_dir)) / ".envrc"
        try:
            digest = hashlib.sha256(envrc.read_bytes()).hexdigest()
        except OSError:
            digest = None
        evidence["environment"] = {"file": str(envrc), "sha256": digest, "validated": validated,
                                   "selects": {"BRAVE_CORE_DIR": str(identity.core),
                                               "BRAVE_SRC_ROOT": str(identity.src)}}
    status = run_capture(["git", "-C", str(identity.core), "status", "--porcelain"], str(identity.core), None, ctx.log,
                         timeout=120)
    evidence["source"] = {
        "core_head": freshness.resolve_head(identity.core, ctx.log),
        "chromium_head": freshness.resolve_head(identity.src, ctx.log),
        "core_uncommitted_files": len(status.stdout.splitlines()) if status.returncode == 0 and not status.truncated
        else None,
        "chromium_uncommitted": "not computed here; the output record's fingerprint covers tracked changes"}
    return evidence


@contextlib.contextmanager
def track(ctx, command, identity, details, validated=False):
    """The operation lifecycle: record before mutation, then finish with the actual outcome.

    Expected failures and cancellations finish the record (with the operation ID attached to the error) and
    are re-raised; a process that dies leaves the record incomplete. Call `op.complete(result)` on success.
    `validated` records that the checkout's approved environment was loaded and checked first.
    """
    op = Operation(command, identity, details, ctx.state_root, describe_start(ctx, identity, validated))
    ctx.log.listeners.append(op.command_dispatched)
    try:
        yield op
    except ScaffoldError as error:
        error.operation_id = op.id
        op.finish("error", error.exit_code, child_exit_code=error.child_exit_code,
                  error={"code": error.code, "message": error.message})
        raise
    except Cancelled as cancelled:
        cancelled.operation_id = op.id
        op.finish("cancelled", cancelled.exit_code, cleanup={"complete": not cancelled.cleanup_incomplete})
        raise
    except Exception as error:
        op.finish("error", 1, error={"code": "INTERNAL_ERROR", "message": str(error)})
        raise
    finally:
        ctx.log.listeners.remove(op.command_dispatched)


def prune(root, keep=KEEP_OPERATIONS):
    """Delete the oldest completed operation records beyond `keep`.

    Incomplete records and any record an output state still references are kept
    because they are recovery evidence. Browser outputs are never touched.
    """
    directory = Path(root) / "operations"
    if not directory.is_dir():
        return
    referenced = set()
    for state_file in (Path(root) / "outputs").glob("*/*.json"):
        state = _read(state_file) or {}
        for entry in [state.get("success"), *state.get("attempts", [])]:
            if entry and entry.get("operation_id"):
                referenced.add(entry["operation_id"])
    records = sorted(directory.glob("*.json"))
    for path in records[:-keep] if len(records) > keep else []:
        data = _read(path) or {}
        if data.get("state") == "incomplete" or data.get("operation_id") in referenced:
            continue
        path.unlink(missing_ok=True)


def incomplete_operations(root=None, checkout=None):
    found = []
    for path in sorted((store_root(root) / "operations").glob("*.json")):
        data = _read(path)
        if data and data.get("state") == "incomplete" and (checkout is None or data.get("checkout") == str(checkout)):
            found.append(data)
    return found


class OutputState:
    """Success and attempt history for one output directory of one checkout."""

    def __init__(self, identity, output_dir, root=None):
        self.output_dir = os.path.realpath(output_dir)
        digest = hashlib.sha1(self.output_dir.encode()).hexdigest()[:16]
        self.path = store_root(root) / "outputs" / checkout_key(identity.core) / (digest + ".json")
        self.data = _read(self.path) or {
            "output_dir": self.output_dir, "checkout": str(identity.core), "needs_revalidation": False,
            "attempts": [], "success": None, "history": []}

    def save(self):
        _write(self.path, self.data)

    def begin_attempt(self, operation_id, changes_output=True):
        """Record an attempt before any write; earlier success stops being proof."""
        self.data["attempts"].append({"operation_id": operation_id, "started": now(), "outcome": "started",
                                      "changes_output": changes_output,
                                      "uncertain_before": bool(self.data["needs_revalidation"])})
        self.data["attempts"] = self.data["attempts"][-20:]
        if changes_output and self.data["success"]:
            self.data["needs_revalidation"] = True
        self.save()

    def end_attempt(self, operation_id, outcome):
        for attempt in self.data["attempts"]:
            if attempt["operation_id"] == operation_id:
                attempt["outcome"] = outcome
        self.save()

    def end_attempt_completed(self, operation_id):
        """An attempt that finished cleanly without producing a new artifact record (a passing test run).

        It gives no evidence about the browser output, so the output is exactly as uncertain as it was
        before the attempt began; only a validated build clears the marker.
        """
        for attempt in self.data["attempts"]:
            if attempt["operation_id"] == operation_id:
                self.data["needs_revalidation"] = attempt.get("uncertain_before", False)
        self.end_attempt(operation_id, "succeeded")

    def record_success(self, operation_id, artifact, fingerprint):
        if self.data["success"]:
            self.data["history"] = [*self.data["history"], self.data["success"]][-10:]
        self.data["success"] = {"operation_id": operation_id, "at": now(), "artifact": artifact,
                                "fingerprint": fingerprint}
        self.data["needs_revalidation"] = False
        self.end_attempt(operation_id, "succeeded")

    @property
    def needs_revalidation(self):
        return bool(self.data["needs_revalidation"])

    @property
    def success(self):
        return self.data["success"]

    def last_attempt(self):
        return self.data["attempts"][-1] if self.data["attempts"] else None

    def last_uncertain_attempt(self):
        """The most recent attempt that did not finish cleanly; it is why the output may be partly overwritten."""
        return next((item for item in reversed(self.data["attempts"]) if item["outcome"] != "succeeded"), None)


def output_states(identity, root=None):
    directory = store_root(root) / "outputs" / checkout_key(identity.core)
    states = []
    for path in sorted(directory.glob("*.json")):
        data = _read(path)
        if data:
            states.append(data)
    return states
