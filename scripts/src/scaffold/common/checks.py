# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Named readiness checks shared by doctor, plans, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from .results import ScaffoldError

PASS, BLOCKER, WARNING, UNSUPPORTED, NOT_CHECKED = "pass", "blocker", "warning", "unsupported", "not_checked"
MARKERS = {PASS: "✅", BLOCKER: "❌", WARNING: "⚠️", UNSUPPORTED: "🚫", NOT_CHECKED: "❔"}
MARKER_LEGEND = "%s pass  %s blocker  %s warning  %s unsupported  %s not checked" % tuple(
    MARKERS[status] for status in (PASS, BLOCKER, WARNING, UNSUPPORTED, NOT_CHECKED))


@dataclass
class CheckResult:
    name: str
    status: str
    summary: str
    scopes: tuple = ()
    required: bool = True
    evidence: dict = field(default_factory=dict)
    affects: tuple = ()
    repairs: list = field(default_factory=list)

    def to_dict(self):
        return {"name": self.name, "status": self.status, "required": self.required,
                "summary": self.summary, "scopes": list(self.scopes), "evidence": self.evidence,
                "affects": list(self.affects), "repairs": self.repairs}


def make_check(name, status, summary, scope, required=True, affects=(), repairs=None, **evidence):
    """Build a CheckResult; keyword arguments become evidence."""
    return CheckResult(name=name, status=status, summary=summary, scopes=(scope,), required=required,
                       evidence=evidence, affects=tuple(affects), repairs=repairs or [])


def aggregate(checks):
    """Return (code, message) when required readiness is missing, else None."""
    required = [check for check in checks if check.required]
    blockers = [check for check in required if check.status in (BLOCKER, UNSUPPORTED)]
    if blockers:
        return "READINESS_BLOCKED", "%d required check(s) are blocked: %s" % (
            len(blockers), ", ".join(check.name for check in blockers))
    unchecked = [check for check in required if check.status == NOT_CHECKED]
    if unchecked:
        return "READINESS_INCOMPLETE", "%d required check(s) could not be evaluated: %s" % (
            len(unchecked), ", ".join(check.name for check in unchecked))
    return None


def readiness_error(checks):
    verdict = aggregate(checks)
    if verdict is None:
        return None
    code, message = verdict
    repairs = []
    for check in checks:
        if check.required and check.status in (BLOCKER, UNSUPPORTED, NOT_CHECKED):
            repairs.extend(check.repairs)
    return ScaffoldError(code, message, details={
        "blocking": [check.name for check in checks if check.required and check.status in (BLOCKER, UNSUPPORTED)],
        "incomplete": [check.name for check in checks if check.required and check.status == NOT_CHECKED]},
        repairs=repairs)
