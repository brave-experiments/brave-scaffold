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
from ..common.revision import read_revision
from ..common.results import Cancelled, ScaffoldError
from . import freshness

KEEP_OPERATIONS = 100


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def store_root(root=None):
    return Path(root or scaffold_root()) / ".bcore"


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

    def __init__(self, command, identity, details, root=None, evidence=None, progress=None):
        self.progress = progress
        self.root = store_root(root)
        self.id = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        self.path = self.root / "operations" / (self.id + ".json")
        self.data = {
            "operation_id": self.id, "scaffold_revision": read_revision(), "command": command, "state": "incomplete",
            "started": now(), "finished": None, "checkout": str(identity.core),
            "chromium_src": str(identity.src), "details": details, "steps": [], "commands": [],
            "artifacts": [], "status": None, "exit_code": None, "child_exit_code": None, "error": None,
            "cleanup": None, **(evidence or {}),
        }
        self.save()

    def save(self):
        _write(self.path, self.data)

    def note(self, name, **fields):
        """Evidence recorded once, such as the plan a phase was judged by; it is not a phase."""
        self.data["steps"].append({"name": name, "at": now(), **fields})
        self.save()

    def start(self, name, **fields):
        """A phase begins. It stays `running` in the saved record until it succeeds or fails, so a process that
        dies mid-phase leaves the evidence of where."""
        if self.progress:
            label = "Running Core build" if name == "build" else name.replace("-", " ").capitalize()
            self.progress(label + "...")
        self.data["steps"].append({**fields, "name": name, "at": now(), "status": "running"})
        self.save()

    def succeed(self, name, **outcome):
        """The phase finished; `outcome` holds the facts callers and errors should keep about it."""
        self._close(name, "succeeded", outcome)

    def fail(self, name, **outcome):
        self._close(name, "failed", outcome)

    def attach_artifacts(self, artifacts):
        """Save the verified artifacts now, so a later phase failing cannot lose them."""
        self.data["artifacts"] = [dict(item) for item in artifacts]
        self.save()

    def _close(self, name, status, outcome):
        step = next((item for item in reversed(self.data["steps"]) if item["name"] == name and
                     item.get("status") == "running"), None)
        if step is None:
            step = {"name": name, "at": now()}
            self.data["steps"].append(step)
        step.update(status=status, finished=now(), outcome=outcome)
        self.save()

    def settle(self, status):
        """Mark phases still running when the operation ends (an error or cancellation) with how it ended."""
        for item in self.data["steps"]:
            if item.get("status") == "running":
                item.update(status=status, finished=now())

    def completed(self):
        """The facts of every phase that finished, oldest first."""
        return [{"phase": item["name"], **item.get("outcome", {})} for item in self.data["steps"]
                if item.get("status") == "succeeded"]

    def detail(self, **fields):
        self.data["details"].update(fields)
        self.save()

    def command_dispatched(self, entry):
        self.data["commands"].append(entry)
        self.save()

    def finish(self, status, exit_code, child_exit_code=None, error=None, cleanup=None):
        """End the record. The phases, artifacts, and commands already saved are its summary; nothing is rebuilt."""
        self.data.update(state="complete", finished=now(), status=status, exit_code=exit_code)
        for name, value in (("child_exit_code", child_exit_code), ("error", error), ("cleanup", cleanup)):
            if value is not None:
                self.data[name] = value
        self.save()
        prune(self.root)

    def complete(self, result):
        """Finish from a command's result and stamp the operation ID on it."""
        result.operation_id = self.id
        if result.artifacts:
            self.attach_artifacts(result.artifacts)
        self.finish(result.status, result.exit_code,
                    child_exit_code=result.child_exit_code, error=result.error and
                    {"code": result.error["code"], "message": result.error["message"]})
        return result


def describe_start(ctx, identity, validated=False):
    """Evidence stored with every operation: environment identity (no values), source state, log destinations."""
    evidence = {"logs": {
        "commands": "stderr" if ctx.log.enabled and ctx.log.verbosity == "verbose" else "diagnostic",
        "child_output": "diagnostic" if ctx.log.verbosity == "quiet" else "stderr" if ctx.json_mode else "terminal",
        "diagnostic": ctx.log.path, "verbosity": ctx.log.verbosity}}
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
    branch = run_capture(["git", "-C", str(identity.core), "symbolic-ref", "--quiet", "--short", "HEAD"],
                         str(identity.core), None, ctx.log, timeout=30)
    evidence["source"] = {
        "core_branch": branch.stdout.strip() if branch.returncode == 0 and not branch.truncated
        and not branch.timed_out else None,
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
    op = Operation(command, identity, details, ctx.state_root, describe_start(ctx, identity, validated), ctx.log.phase)
    ctx.log.listeners.append(op.command_dispatched)
    try:
        yield op
    except ScaffoldError as error:
        op.settle("failed")
        error.operation_id = op.id
        if op.completed():
            error.details.setdefault("completed_phases", op.completed())
        error.artifacts = op.data["artifacts"]
        op.finish("error", error.exit_code, child_exit_code=error.child_exit_code,
                  error={"code": error.code, "message": error.message})
        raise
    except Cancelled as cancelled:
        op.settle("interrupted")
        cancelled.operation_id = op.id
        cancelled.completed_phases = op.completed()
        op.finish("cancelled", cancelled.exit_code, cleanup={"complete": not cancelled.cleanup_incomplete})
        raise
    except Exception as error:
        op.settle("failed")
        op.finish("error", 1, error={"code": "INTERNAL_ERROR", "message": str(error)})
        raise
    finally:
        ctx.log.listeners.remove(op.command_dispatched)


def cleanup_remainders(record):
    """Owned private directories that a cleanup did not finish removing."""
    if record.get("command") != "clean":
        return []
    fields = ("directory", "private", "identity", "out_dir")
    return [{key: step[key] for key in fields} for step in record.get("steps", [])
            if step.get("name") == "delete" and step.get("status") in ("running", "interrupted", "failed")
            and all(step.get(key) for key in fields)]


def prune(root, keep=KEEP_OPERATIONS):
    """Delete the oldest completed operation records beyond `keep`.

    Incomplete records, output-state references, and existing cleanup remainders
    keep their recovery evidence. Browser outputs are never touched.
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
        remaining = any(os.path.lexists(Path(item["out_dir"]) / item["private"])
                        for item in cleanup_remainders(data))
        if data.get("state") == "incomplete" or data.get("operation_id") in referenced or remaining:
            continue
        path.unlink(missing_ok=True)


def all_operations(root=None, checkout=None, command=None):
    """Saved operation records, oldest first, optionally for one checkout and command."""
    for path in sorted((store_root(root) / "operations").glob("*.json")):
        data = _read(path)
        if data and (checkout is None or data.get("checkout") == str(checkout)) and \
                (command is None or data.get("command") == command):
            yield data


def incomplete_operations(root=None, checkout=None):
    return [data for data in all_operations(root, checkout) if data.get("state") == "incomplete"]


class OutputState:
    """Success and attempt history for one output directory of one checkout."""

    def __init__(self, identity, output_dir, root=None):
        self.output_dir = os.path.realpath(output_dir)
        digest = hashlib.sha1(self.output_dir.encode()).hexdigest()[:16]
        self.path = store_root(root) / "outputs" / checkout_key(identity.core) / (digest + ".json")
        loaded = _read(self.path)
        # An unreadable file hides what the output went through, so the output counts as uncertain.
        self.damaged = self.path.exists() and not _usable_state(loaded)
        self.data = loaded if _usable_state(loaded) else {
            "output_dir": self.output_dir, "checkout": str(identity.core), "needs_revalidation": self.damaged,
            "attempts": [], "success": None, "history": []}

    def save(self):
        if self.damaged:
            aside = self.path.with_name("%s.damaged-%s" % (self.path.name, time.strftime("%Y%m%dT%H%M%S")))
            with contextlib.suppress(OSError):
                os.replace(self.path, aside)
            self.damaged = False
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


def _usable_state(data):
    return isinstance(data, dict) and bool(data)


def output_states(identity, root=None):
    directory = store_root(root) / "outputs" / checkout_key(identity.core)
    states = []
    for path in sorted(directory.glob("*.json")):
        data = _read(path)
        if _usable_state(data):
            states.append(data)
    return states


def damaged_output_records(identity, root=None):
    """Paths of output-state files that exist but cannot be read; their outputs count as uncertain."""
    directory = store_root(root) / "outputs" / checkout_key(identity.core)
    return [str(path) for path in sorted(directory.glob("*.json")) if not _usable_state(_read(path))]
