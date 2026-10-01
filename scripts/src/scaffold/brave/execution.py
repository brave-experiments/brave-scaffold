# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The selected checkout's execution context: identity, approved environment, and resolved tools.

A command context holds the caller's options, configuration, and environment. An `Execution` holds what
was established for one checkout: its frozen identity, its approved environment loaded and checked against
that identity, and the tools resolved inside it. Execution and checkout-scoped readiness both read it, so a
check and the command it protects see the same environment. It is a snapshot: tools and readiness are
resolved again explicitly after a step, such as a sync, that changes the checkout.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from ..common import env as env_module
from ..common import tools as tools_module


@dataclass(frozen=True)
class Execution:
    identity: object
    environ: dict
    toolchain: object = None
    checks: tuple = ()  # every check that has been judged for this execution, in order

    def context(self, ctx):
        """The command context as the platform code written against `ctx.environ` expects it: same options,
        configuration, and log, with the checkout's environment in place of the caller's."""
        return dataclasses.replace(ctx, environ=self.environ)

    def with_checks(self, checks):
        return dataclasses.replace(self, checks=(*self.checks, *checks))


def load(ctx, identity):
    """Load and validate the checkout's approved environment (evaluates the reviewed `.envrc`)."""
    ctx.log.phase("Checking environment for %s..." % identity.core)
    with ctx.log.measure("Environment"):
        return Execution(identity, env_module.load_environment(identity, ctx.environ, ctx.log))


def resolve_tools(execution, ctx):
    """Resolve the checkout-local tools now; stops with `LOCAL_TOOL_MISSING` unless they are all ready."""
    toolchain, checks = tools_module.require_toolchain(execution.identity, ctx.log)
    return dataclasses.replace(execution, toolchain=toolchain).with_checks(checks)
