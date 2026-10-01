# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Reviewed support script identities and their complete mutation manifests.

The manifest belongs to the tooling installation. A support repository cannot
approve its own shell code by declaring what that code writes.
"""

import json
from pathlib import Path

from ..common.results import ScaffoldError
from .patch_inventory import sha256_or_none

MANIFEST = Path(__file__).with_name("support_script_contracts.json")


def require_contract(wc, script):
    path = Path(wc) / script
    digest = None if path.is_symlink() else sha256_or_none(path)
    contracts = json.loads(MANIFEST.read_text(encoding="utf-8"))
    contract = contracts.get(digest)
    if contract is None or contract["script"] != script:
        raise ScaffoldError(
            "PREPARATION_CONFLICT",
            "%s has no reviewed script identity and complete write manifest; it was not executed." % script,
            details={"files": [{"path": script, "reason": "unsupported script identity", "sha256": digest}]})
    return contract


def require_contracts(wc):
    return {name: require_contract(wc, name) for name in ("applyPatches.sh", "copyMacRes.sh")}
