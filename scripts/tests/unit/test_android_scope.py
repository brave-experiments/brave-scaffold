# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The support patch script interface: which repository each patch is applied in."""

import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import android_deps

SCRIPT = """#!/usr/bin/env bash
src_root=$(cd ../src && pwd -P)
patch_a="$current_dir/patches/first.patch"
patch_v8="$current_dir/patches/v8-only.patch"
handle_patch() {
  echo "$2"
}
handle_patch "First" "$src_root" "$patch_a" || failures=1
android_host_assert || failures=1
handle_patch "V8" "$src_root/v8" "$patch_v8" || failures=1
handle_patch "Literal" "${src_root}/third_party/deep/repo" "$PWD/patches/literal.patch"
"""


def applications(script):
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "applyPatches.sh").write_text(script)
        return android_deps.patch_applications(Path(directory))


class PatchCallTests(unittest.TestCase):
    def test_each_call_names_its_repository_and_patch(self):
        found, problems = applications(SCRIPT)
        self.assertEqual(problems, [])
        self.assertEqual(found, [("", "first.patch"), ("v8", "v8-only.patch"),
                                 ("third_party/deep/repo", "literal.patch")])

    def test_a_script_without_patch_calls_has_no_declared_interface(self):
        self.assertEqual(applications("#!/bin/bash\ncp a b\n"), (None, []))

    def test_a_call_the_interface_cannot_resolve_is_a_problem_not_a_guess(self):
        for call in ('handle_patch "x" "$other_root" "$PWD/patches/a.patch"', 'handle_patch "x" "$src_root" "$unknown"',
                     'handle_patch "x" "$src_root"', 'handle_patch "x" "$src_root/../out" "$PWD/patches/a.patch"'):
            with self.subTest(call=call):
                found, problems = applications("#!/bin/bash\n" + call + "\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertEqual(found, None)


if __name__ == "__main__":
    unittest.main()
