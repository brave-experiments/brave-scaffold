# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android-on-Mac support stays optional on other host platforms."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests.support  # noqa: F401
from scaffold.brave import android, android_checks, android_tests, registry
from scaffold.common.results import ScaffoldError


class PlatformTests(unittest.TestCase):
    def test_other_hosts_neither_prepare_nor_inspect_macos_support(self):
        ctx = SimpleNamespace(environ={})
        for host in ("linux", "windows"):
            with self.subTest(host=host), patch.object(android, "host_platform", return_value=host), \
                    patch.object(android_checks, "host_platform", return_value=host), \
                    patch.object(android_checks.adb, "find_adb", return_value=None), \
                    patch.object(android_checks, "run_capture", side_effect=AssertionError("unexpected LFS probe")):
                self.assertEqual(android_checks.support_checks(ctx, "android"), [])
                self.assertEqual(len(android_checks.machine_checks(ctx, "android")), 1)
                with self.assertRaises(ScaffoldError) as failure:
                    android.cmd_android_setup(ctx)
                self.assertEqual(failure.exception.code, "UNSUPPORTED_CAPABILITY")


class TestBranchTests(unittest.TestCase):
    def test_renamed_branch_updates_validation_repair_and_help(self):
        with patch.object(android_tests, "TEST_SUPPORT_BRANCH", "android-test-support"), \
                patch.object(android_tests.android_deps, "working_copy", return_value="/support"), \
                patch.object(android_tests.android_deps, "inspect_working_copy",
                             return_value={"branch": "old-branch", "head": "123"}):
            with self.assertRaises(ScaffoldError) as caught:
                android_tests.require_support_branch(SimpleNamespace(core="/core"))
            error = caught.exception
            self.assertEqual(error.details["required_branch"], "android-test-support")
            self.assertIn("android-test-support", error.message)
            self.assertIn("android-test-support", str(error.repairs))
            test = registry.build_registry()["test"]
            self.assertIn("android-test-support branch", test.side_effects)
