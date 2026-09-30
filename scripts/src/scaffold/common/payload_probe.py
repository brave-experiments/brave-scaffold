# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only probe: does the checkout's payload metadata say an entry is deployed?

Usage: payload_probe.py <core> <workspace> <entry>
Prints {"state": "current" | "stale" | "unverified"}. Checkouts without payload
metadata report "unverified"; nothing is downloaded or written.
"""

import importlib.util
import json
import os
import sys


def main():
    core, workspace, entry = sys.argv[1:4]
    module_path = os.path.join(core, "tools", "cr", "extra_deps.py")
    if not os.path.isfile(module_path):
        return "unverified"
    spec = importlib.util.spec_from_file_location("checkout_extra_deps", module_path)
    if spec is None or spec.loader is None:
        return "unverified"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        installed = module.check_extra_deps_installed(__import__("pathlib").Path(workspace), entry)
    except (OSError, SyntaxError, ValueError, KeyError, AttributeError, ImportError):
        return "unverified"
    return "current" if installed else "stale"


if __name__ == "__main__":
    print(json.dumps({"state": main()}))
