# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Dispatch from a launcher to the matching command line."""

import sys


def run_tool(tool, argv):
    if tool == "bdev":
        from .brave.bdev import main
    elif tool == "sync-support-repos":
        from .support import main
    elif tool == "bpm":
        from .brave.bpm import main
    elif tool == "git-sign-with-1password":
        from .brave.signer import main
    else:
        sys.stderr.write("Unknown tool %r\n" % tool)
        return 2
    return main(argv)
