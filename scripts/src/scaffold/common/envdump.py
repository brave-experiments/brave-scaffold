# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Child process that records the environment direnv loaded, then exits."""

import json
import os
import sys


def main():
    with open(sys.argv[1], "w", encoding="utf-8") as stream:
        json.dump({"environ": dict(os.environ), "cwd": os.getcwd()}, stream)


if __name__ == "__main__":
    main()
