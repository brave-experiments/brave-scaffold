# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Result envelope, exit codes, and error types shared by every command."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

from .redaction import redact_report, redact_url_credentials

SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INPUT = 2
EXIT_SETUP = 3
EXIT_CONFLICT = 4
EXIT_CHILD = 5
EXIT_PARTIAL = 6
EXIT_SIGINT = 130
EXIT_SIGTERM = 143

CODE_EXIT = {
    "INVALID_INPUT": EXIT_INPUT,
    "CHECKOUT_REQUIRED": EXIT_INPUT,
    "CHECKOUT_AMBIGUOUS": EXIT_INPUT,
    "CHECKOUT_NOT_FOUND": EXIT_INPUT,
    "UNSUPPORTED_CAPABILITY": EXIT_INPUT,
    "DEVICE_AMBIGUOUS": EXIT_INPUT,
    "ARTIFACT_AMBIGUOUS": EXIT_INPUT,
    "CONFIG_INVALID": EXIT_INPUT,
    "SELECTOR_CONFLICT": EXIT_INPUT,
    "ENVIRONMENT_REQUIRED": EXIT_SETUP,
    "ENVIRONMENT_UNAPPROVED": EXIT_SETUP,
    "ENVIRONMENT_LOAD_FAILED": EXIT_SETUP,
    "CHECKOUT_ENV_CONFLICT": EXIT_SETUP,
    "LOCAL_TOOL_MISSING": EXIT_SETUP,
    "READINESS_BLOCKED": EXIT_SETUP,
    "READINESS_INCOMPLETE": EXIT_SETUP,
    "DEPENDENCY_INCOMPATIBLE": EXIT_SETUP,
    "DEVICE_UNAVAILABLE": EXIT_SETUP,
    "PREPARATION_CONFLICT": EXIT_CONFLICT,
    "OWNERSHIP_CONFLICT": EXIT_CONFLICT,
    "CHILD_FAILED": EXIT_CHILD,
    "ARTIFACT_MISSING": EXIT_CHILD,
    "ARTIFACT_MISMATCH": EXIT_CHILD,
    "ARTIFACT_UNRESOLVED": EXIT_CHILD,
    "LAUNCH_FAILED": EXIT_CHILD,
}


def repair(argv, cwd=None, requires_user_action=False, note=None):
    """One suggested next step. Suggestions never run automatically."""
    record = {"argv": list(argv), "cwd": cwd, "requires_user_action": requires_user_action}
    if note:
        record["note"] = note
    return record


class ScaffoldError(Exception):
    """A reportable failure with a stable code and an actionable message."""

    def __init__(self, code, message, details=None, repairs=None, exit_code=None,
                 child_exit_code=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.repairs = repairs or []
        self.exit_code = CODE_EXIT.get(code, EXIT_INTERNAL) if exit_code is None else exit_code
        self.child_exit_code = child_exit_code
        self.operation_id = None
        self.artifacts = []  # verified artifacts of phases that finished before this failure


class Cancelled(BaseException):
    """Raised from a signal handler; carries the conventional exit code."""

    def __init__(self, exit_code):
        super().__init__(exit_code)
        self.exit_code = exit_code
        self.cleanup_incomplete = False
        self.operation_id = None
        self.completed_phases = []


@dataclass
class Result:
    command: str
    status: str = "ok"
    exit_code: int = EXIT_OK
    context: dict = field(default_factory=lambda: {"checkout": None, "selection_source": None})
    data: Any = None
    checks: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    error: dict | None = None
    artifacts: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    operation_id: str | None = None
    child_exit_code: int | None = None
    text: str | None = None

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "command": self.command,
            "operation_id": self.operation_id,
            "context": self.context,
            "data": self.data,
            "checks": self.checks,
            "warnings": self.warnings,
            "error": self.error,
            "artifacts": self.artifacts,
            "logs": self.logs,
            "exit_code": self.exit_code,
            "child_exit_code": self.child_exit_code,
        }

    def redacted(self):
        """A copy with secrets removed from everything that is shown or stored."""
        safe = {name: redact_report(getattr(self, name)) for name in (
            "context", "data", "checks", "warnings", "error", "artifacts", "logs")}
        text = redact_url_credentials(self.text) if self.text else self.text
        return Result(command=self.command, status=self.status, exit_code=self.exit_code,
                      operation_id=self.operation_id, child_exit_code=self.child_exit_code, text=text, **safe)

    def add_warning(self, code, message, **details):
        record = {"code": code, "message": message}
        if details:
            record["details"] = details
        self.warnings.append(record)


def error_result(command, error, context=None):
    result = Result(command=command, status="error", exit_code=error.exit_code,
                    child_exit_code=error.child_exit_code, operation_id=error.operation_id,
                    artifacts=list(error.artifacts))
    if context:
        result.context = context
    result.error = {"code": error.code, "message": error.message,
                    "details": error.details, "repairs": error.repairs}
    return result


def render_error_text(error):
    lines = ["❌ Error [%s]: %s" % (error["code"], error["message"])]
    details = error.get("details") or {}
    lines.extend(detail_lines(details))
    for step in error.get("repairs") or []:
        suffix = "  (requires you to act)" if step.get("requires_user_action") else ""
        note = "  # %s" % step["note"] if step.get("note") else ""
        command = _shell_join(step["argv"])
        if step.get("cwd"):
            command = "cd %s && %s" % (_shell_join([step["cwd"]]), command)
        line = "  Next: %s%s%s" % (command, suffix, note)
        if line not in lines:
            lines.append(line)
    return "\n".join(lines)


def detail_lines(details, limit=12):
    """Bounded plain text; complete structured evidence stays in JSON and logs."""
    lines = []
    omitted = False

    def add(line):
        nonlocal omitted
        if line in lines:
            return
        line = line.replace("\n", " ").replace("\r", " ")
        if len(line) > 700:
            line = line[:700] + "..."
            omitted = True
        if len(lines) < limit:
            lines.append(line)
        else:
            omitted = True

    def visit(label, value, depth=0):
        nonlocal omitted
        if depth > 2:
            add("  %s: details available with --json" % label)
        elif isinstance(value, dict):
            if "path" in value and "reason" in value:
                add("  %s: %s" % (value["path"], value["reason"]))
                if value.keys() - {"path", "reason"}:
                    omitted = True
            elif {"name", "status", "summary"} <= value.keys():
                from .checks import display_label
                add("  %s [%s]: %s" % (display_label(value["name"]), value["status"], value["summary"]))
                if value.keys() - {"name", "status", "summary"}:
                    omitted = True
            else:
                for key, item in value.items():
                    visit("%s / %s" % (label, key.replace("_", " ")), item, depth + 1)
        elif isinstance(value, (list, tuple)):
            if label in ("argv", "arguments", "options"):
                add("  %s: %s" % (label, _shell_join(value)))
            else:
                for item in value:
                    visit(label, item, depth + 1)
        elif value is not None:
            if label in ("blocking", "incomplete"):
                from .checks import display_label
                value = display_label(str(value))
            add("  %s: %s" % (label, value))

    # Show path bases and backup locations before a long file list fills the limit.
    first = ("path_base", "checkout", "repository", "backup", "cwd")
    for key in dict.fromkeys([*(key for key in first if key in details), *details]):
        visit(key.replace("_", " "), details[key])
    if omitted:
        lines.append("  More details available with --json and in the diagnostic log.")
    return lines


def _shell_join(argv):
    import shlex
    return " ".join(shlex.quote(str(part)) for part in argv)


def emit(result, json_mode, stdout=None, stderr=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    result = result.redacted()
    if json_mode:
        stdout.write(json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n")
        return
    if result.text:
        stdout.write(result.text.rstrip("\n") + "\n")
    doctor_report = result.command == "doctor" and result.text and result.checks
    for warning in result.warnings:
        if doctor_report and warning["code"].startswith("CHECK_"):
            continue
        stderr.write("Warning [%s]: %s\n" % (warning["code"], warning["message"]))
    if result.error and not (doctor_report and result.error["code"] in ("READINESS_BLOCKED", "READINESS_INCOMPLETE")):
        stderr.write(render_error_text(result.error) + "\n")
    stdout.flush()
    stderr.flush()
