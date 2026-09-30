# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Entry point shared by the launchers.

This file must stay parseable by older interpreters so a too-old runtime gets a
clear diagnostic instead of a syntax error.
"""

import os
import sys

MINIMUM = (3, 14)


def main():
    tool = sys.argv[1]
    if sys.version_info < MINIMUM:
        home = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.stderr.write(
            "%s: the scaffold runtime is Python %d.%d; Python 3.14 or newer is required.\n"
            "Recreate only this virtual environment:\n"
            "  python3.14 -m venv --clear --without-pip '%s/.venv'\n"
            % (tool, sys.version_info[0], sys.version_info[1], home))
        return 3
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scaffold.entry import run_tool
    return run_tool(tool, sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
