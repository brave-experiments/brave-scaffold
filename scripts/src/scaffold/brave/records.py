# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Operation records and per-output build state, kept outside Brave Core."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from ..common.config import atomic_write, scaffold_root
from ..common.redaction import redact_report

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

    An operation that dies without finishing keeps `state: "incomplete"`, so a
    later inspection can report the interruption instead of claiming success.
    """

    def __init__(self, command, identity, details, root=None):
        self.root = store_root(root)
        self.id = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        self.path = self.root / "operations" / (self.id + ".json")
        self.data = {
            "operation_id": self.id, "cli_version": CLI_VERSION, "command": command, "state": "incomplete",
            "started": now(), "finished": None, "checkout": str(identity.core),
            "chromium_src": str(identity.src), "details": details, "steps": [], "commands": [],
            "artifacts": [], "status": None, "exit_code": None,
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

    def finish(self, status, exit_code, commands=None, artifacts=None):
        self.data.update(state="complete", finished=now(), status=status, exit_code=exit_code)
        if commands is not None:
            self.data["commands"] = commands
        if artifacts is not None:
            self.data["artifacts"] = artifacts
        self.save()
        prune(self.root)


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
                                      "changes_output": changes_output})
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
        """An attempt that wrote the output and finished cleanly without producing a new artifact record.

        The output is consistent again, so it no longer needs revalidation; the earlier
        success record keeps its own input fingerprint, which still decides staleness.
        """
        self.data["needs_revalidation"] = False
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


def output_states(identity, root=None):
    directory = store_root(root) / "outputs" / checkout_key(identity.core)
    states = []
    for path in sorted(directory.glob("*.json")):
        data = _read(path)
        if data:
            states.append(data)
    return states
