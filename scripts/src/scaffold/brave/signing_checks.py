# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only checks of user-level Git signing setup. Nothing is written or printed secretly."""

from __future__ import annotations

import os
from pathlib import Path

from ..common.checks import BLOCKER, PASS, WARNING, CheckResult
from ..common.procs import run_capture
from . import signer

SOCKET_CANDIDATES = ("~/.1password/agent.sock",
                     "~/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock")


def _git_config(ctx, key):
    # Read the effective configuration for this repository, never for a checkout.
    result = run_capture(["git", "config", "--get", key], str(ctx.scaffold_root), ctx.environ, ctx.log,
                         timeout=15)
    return result.stdout.strip() if result.returncode == 0 else None


def signing_checks(ctx, scope):
    def check(name, status, summary, required=True, affects=("signed commits",), **evidence):
        return CheckResult(name=name, status=status, summary=summary, scopes=(scope,), required=required,
                           evidence=evidence, affects=tuple(affects))

    checks = []
    fmt = _git_config(ctx, "gpg.format")
    checks.append(check("git-signing-format", PASS if fmt == "ssh" else BLOCKER,
                        "gpg.format = ssh" if fmt == "ssh" else
                        "gpg.format is %r in your Git configuration; SSH signing is expected." % fmt))
    program = _git_config(ctx, "gpg.ssh.program")
    launcher = ctx.scaffold_root / "scripts" / "git-sign-with-1password"
    if program and os.access(os.path.expanduser(program), os.X_OK):
        status, text = PASS, "gpg.ssh.program is executable: %s" % program
    else:
        status, text = BLOCKER, "gpg.ssh.program is unset or not executable: %r" % program
    checks.append(check("signer-program", status, text, program=program,
                        installation_signer=str(launcher)))
    key = _git_config(ctx, "user.signingkey")
    checks.append(check("signing-key", PASS if key else BLOCKER,
                        "user.signingkey is configured." if key else "user.signingkey is not configured."))
    auto = _git_config(ctx, "commit.gpgsign")
    checks.append(check("commit-signing-default", PASS if auto == "true" else WARNING,
                        "commit.gpgsign = true" if auto == "true" else
                        "commit.gpgsign is not true; commits are signed only with -S.", required=False))
    app = Path(signer.SIGNER)
    checks.append(check("op-ssh-sign", PASS if os.access(app, os.X_OK) else BLOCKER,
                        "1Password signer present." if os.access(app, os.X_OK)
                        else "1Password's op-ssh-sign was not found at %s." % app))
    sockets = [p for p in (ctx.environ.get("SSH_AUTH_SOCK"), *[os.path.expanduser(s) for s in SOCKET_CANDIDATES])
               if p and os.path.exists(p)]
    checks.append(check("agent-socket", PASS if sockets else WARNING,
                        "An SSH agent socket exists." if sockets else
                        "No SSH agent socket found; signing may open 1Password and retry once.",
                        required=False, sockets=sockets))
    return checks
