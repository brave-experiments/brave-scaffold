# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android build, install, and restart."""

from ..common.results import ScaffoldError


def require_available():
    raise ScaffoldError("UNSUPPORTED_CAPABILITY", "Android is not available yet.")
