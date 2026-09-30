# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cleanup of generated build output."""

from ..common.cli import CommandSpec
from ..common.results import ScaffoldError


def run_clean(ctx):
    raise ScaffoldError("UNSUPPORTED_CAPABILITY", "clean is not implemented yet.")


SPEC = CommandSpec("clean", "List owned build outputs, or delete them with --execute.", run_clean)
