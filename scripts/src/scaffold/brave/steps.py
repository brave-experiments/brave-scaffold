# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Step descriptions shared by plans and operation records.

A step names what an operation reads and writes, the command it dispatches, what it needs first, what happens
when it fails, and what cleanup exists. Plans show them before anything runs; execution records the same
description for the steps that change something, so the two cannot drift apart.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..common.checks import BLOCKER, NOT_CHECKED, WARNING, readiness_error

NO_CLEANUP = "None; the scaffold does not roll back or clean up after a failure."


@dataclass
class Step:
    name: str
    summary: str
    status: str = "planned"
    reads: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    argv: list | None = None
    cwd: str | None = None
    needs: list = field(default_factory=list)
    on_failure: str = ""
    cleanup: str = ""
    detail: str | None = None

    def to_dict(self):
        return asdict(self)

    def record(self):
        """The fields stored in an operation record (its name is the step's key)."""
        data = self.to_dict()
        data.pop("name")
        return data


def render_plan(command, steps):
    lines = ["Plan for %s (nothing was run):" % command]
    for index, step in enumerate(steps, 1):
        line = "  %d. %s [%s] %s" % (index, step.name, step.status, step.summary)
        if step.detail:
            line += " - " + step.detail
        lines.append(line)
    return "\n".join(lines)


def environment_step(identity, error=None):
    record = identity.record
    envrc = Path(record.direnv_dir) / ".envrc" if record is not None and record.direnv_dir else None
    return Step("environment", "Load the approved environment and check that it selects this checkout.",
                "blocked" if error else "ready", reads=[str(envrc)] if envrc else [],
                on_failure="Nothing is prepared or started; approving the environment is yours to do.",
                detail=error.message if error else None)


def tools_step(identity, toolchain, checks):
    failing = [check.summary for check in checks if check.status == BLOCKER]
    return Step("tools", "Resolve the checkout-local Node, package manager, and vpython3 without changing anything.",
                "ready" if toolchain is not None else "blocked", reads=[str(identity.core / "third_party" / "node")],
                needs=["environment"], on_failure="Nothing is started; repair is an explicit, separate command.",
                detail="; ".join(failing) or None)


def readiness_step(checks, note=""):
    error = readiness_error(checks)
    problems = (error.details["blocking"] + error.details["incomplete"]) if error else []
    warned = [check.name for check in checks if check.status == WARNING or check.status == NOT_CHECKED]
    detail = ("blocked: " + ", ".join(problems)) if problems else None
    if warned and not problems:
        detail = "optional or unchecked: " + ", ".join(warned)
    return Step("readiness", "Check the machine and checkout prerequisites for this request. %s" % note,
                "blocked" if error else "ready", reads=["machine tools", "checkout configuration"], needs=["tools"],
                on_failure="Nothing is prepared; each blocker names an explicit repair.", detail=detail)


def patches_step(identity, plan, argv):
    """Core patch preparation; `plan` is a patch plan or the error that stopped planning."""
    if isinstance(plan, Exception):
        return Step("patch-preparation", "Apply Core patches only when needed and only when no local work is at risk.",
                    "blocked", needs=["readiness"], detail=getattr(plan, "message", str(plan)))
    status = {"current": "current", "apply": "planned", "conflict": "blocked"}[plan.action]
    detail = plan.reason
    if plan.action == "conflict":
        detail += ": " + ", ".join(item["path"] for item in plan.conflicts[:10])
    return Step("patch-preparation", "Apply Core patches only when needed and only when no local work is at risk.",
                status, reads=[str(identity.core / "patches"), "patched Chromium files"],
                writes=sorted(plan.report.files)[:50] if plan.action == "apply" else [],
                argv=argv if plan.action == "apply" else None, cwd=str(identity.core) if plan.action == "apply" else None,
                needs=["readiness"], detail=detail,
                on_failure="Stops before the build; files it had already patched stay patched.", cleanup=NO_CLEANUP)


def sync_step(identity, arguments, argv, needs=()):
    return Step("sync", "Sync sources and dependencies with Core's own sync command.", "planned",
                reads=[str(identity.workspace / ".gclient")], writes=["Chromium and Core sources and dependencies",
                                                                        str(identity.workspace / ".gclient")],
                argv=argv, cwd=str(identity.core), needs=list(needs),
                on_failure="Stops before any later phase; a partial sync stays as it is.", cleanup=NO_CLEANUP,
                detail="arguments: " + " ".join(arguments))


def build_step(identity, effective, subcommand, arguments, argv, needs):
    return Step(subcommand, "Run Core's %s command for %s %s %s." % (
        subcommand, effective.target, effective.configuration, effective.arch), "planned",
        reads=[str(identity.core), "patched Chromium sources"], writes=[str(effective.output_dir)],
        argv=argv, cwd=str(identity.core), needs=list(needs),
        on_failure="The output is marked as needing revalidation; the previous build record is kept as history, "
                   "and the output cannot be restored to its earlier state.",
        cleanup=NO_CLEANUP + " Cleaning output is a separate, explicit command.",
        detail=None if argv else "arguments: " + " ".join(arguments) + " (the final command needs local tools)")


def verify_step(effective, needs):
    expectation = "an Android APK" if effective.target == "android" else "the Brave application"
    return Step("verify-output", "Check that the build produced %s in the resolved output." % expectation, "planned",
                reads=[str(effective.output_dir)], needs=list(needs),
                on_failure="No artifact is recorded; combined commands stop before touching a device or a running "
                           "browser.", detail="output: %s" % effective.output_dir)


def support_step(identity, plan, writes):
    """Android support preparation; `plan` is a support plan or the error that stopped planning."""
    summary = "Prepare the Android-on-Mac support patches and resources for this checkout."
    if isinstance(plan, Exception):
        return Step("android-support", summary, "blocked", needs=["patch-preparation"],
                    detail=getattr(plan, "message", str(plan)))
    status = {"current": "current", "refresh": "planned", "conflict": "blocked"}[plan.action]
    detail = plan.reason
    if plan.action == "conflict":
        detail += ": " + ", ".join(item["path"] for item in plan.conflicts[:10])
    refresh = plan.action == "refresh"
    working_copy = str(plan.evidence["working_copy"]["path"])
    scripts = [["bash", "./" + script] for script in plan.scripts]
    if refresh and len(scripts) > 1:
        detail += "; then " + "; then ".join(" ".join(script) for script in scripts[1:])
    return Step("android-support", summary, status, reads=[working_copy], writes=writes if refresh else [],
                argv=scripts[0] if refresh and scripts else None, cwd=working_copy if refresh else None,
                needs=["patch-preparation"],
                on_failure="Stops before the build; scripts that already ran leave their changes in place.",
                cleanup=NO_CLEANUP, detail=detail)


def gn_step(effective, args_gn, skipped):
    return Step("gn-overrides", "Keep the scaffold's marked block of GN overrides at the end of args.gn.", "planned",
                reads=[str(args_gn)], writes=[str(args_gn)], needs=["android-support"],
                on_failure="The build does not start.", cleanup=NO_CLEANUP,
                detail=("left out because you chose them: " + ", ".join(sorted(skipped))) if skipped else None)


def select_device_step(device=None, error=None):
    summary = "Choose one Android device before anything is built or installed."
    if error is not None:
        detail = error.message
        if error.details.get("example"):
            detail += " (for example %s)" % error.details["example"]
        return Step("select-device", summary, "unresolved", reads=["adb devices"], detail=detail,
                    on_failure="Nothing is built or installed.")
    return Step("select-device", summary, "resolved", reads=["adb devices"], on_failure="Nothing is built or installed.",
                detail="device %s (chosen by %s)" % (device["id"], device["source"]))


def install_apk_step(device_id, apk):
    return Step("install-apk", "Install the APK over the existing app on that device; app data is kept.", "planned",
                writes=["device %s: the installed app" % device_id],
                argv=["adb", "-s", device_id, "install", "-d", "-r", "-g", apk], needs=["select-device"],
                on_failure="The restart stops; the previously installed app is left as the install left it.",
                cleanup=NO_CLEANUP)


def stop_package_step(device_id, package):
    return Step("stop-package", "Stop only this package on that device.", "planned",
                writes=["device %s: the running %s process" % (device_id, package)],
                argv=["adb", "-s", device_id, "shell", "am", "force-stop", package], needs=["install-apk"],
                on_failure="The restart is reported as failed and the app is not launched.", cleanup=NO_CLEANUP)


def launch_package_step(device_id, package):
    return Step("launch-package", "Launch the package and confirm its process appears.", "planned",
                writes=["device %s: a new %s process" % (device_id, package)],
                argv=["adb", "-s", device_id, "shell", "monkey", "-p", package, "1"], needs=["stop-package"],
                on_failure="Reported as LAUNCH_FAILED.", cleanup=NO_CLEANUP)


def stop_instances_step(bundle_id, running, needs=()):
    detail = ("running now: %s" % ", ".join("pid %d" % pid for pid in running)) if running else "none running now"
    return Step("stop-running-instances", "Quit every running instance of the same application (any checkout), "
                "escalating from a polite quit to termination to a forced kill.", "planned",
                reads=["running processes"], writes=[], needs=list(needs),
                on_failure="Reported as LAUNCH_FAILED and nothing is launched; if a preflight failed earlier, "
                           "nothing is stopped.", cleanup=NO_CLEANUP,
                detail="application %s; %s" % (bundle_id or "of the built output", detail))


def launch_step(bundle_path, exact, needs=()):
    return Step("launch", "Launch %s and confirm its process appeared." % (
        "exactly the artifact this build produced" if exact else "the selected application"), "planned",
        argv=["open", bundle_path], needs=list(needs), on_failure="Reported as LAUNCH_FAILED.", cleanup=NO_CLEANUP,
        detail=None)
