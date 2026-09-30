# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Freshness of an existing output, as reported by run and deploy."""

from __future__ import annotations

from . import android_deps, freshness, patches
from .records import OutputState


def artifact_freshness(ctx, identity, output_dir, android=False):
    state = OutputState(identity, output_dir, ctx.state_root)
    report = patches.collect_drift(identity)
    recorded = (state.success or {}).get("fingerprint")
    current = freshness.compute(identity, report.patched_paths, [], ctx.log,
                                android_deps.freshness_inputs(identity, ctx.log) if android else None)
    return freshness.assess(recorded, current, state)


def add_freshness_warning(result, assessment):
    if assessment["status"] == "stale":
        result.add_warning("STALE_BUILD", "This output does not include the latest changes: %s" %
                           "; ".join(assessment["evidence"]), freshness=assessment)
    elif assessment["status"] == "unknown":
        result.add_warning("UNKNOWN_FRESHNESS", freshness.UNKNOWN_MESSAGE, freshness=assessment)
