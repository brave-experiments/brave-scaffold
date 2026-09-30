# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Target selection precedence and capability reporting."""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from scaffold.common import platforms
from scaffold.common.results import ScaffoldError


def config(default=None):
    return SimpleNamespace(default_platform=default)


class TargetSelectionTests(unittest.TestCase):
    def test_explicit_beats_configured_beats_host(self):
        with mock.patch.object(platforms.sys, "platform", "darwin"):
            self.assertEqual(platforms.effective_target(None, config()), ("mac", "host"))
            self.assertEqual(platforms.effective_target(None, config("android")), ("android", "configured"))
            self.assertEqual(platforms.effective_target("macos", config("android")), ("mac", "explicit"))
            self.assertEqual(platforms.effective_target("MAC", config()), ("mac", "explicit"))

    def test_unsupported_host_asks_for_an_explicit_target_instead_of_guessing(self):
        with mock.patch.object(platforms.sys, "platform", "linux"):
            with self.assertRaises(ScaffoldError) as caught:
                platforms.effective_target(None, config())
            self.assertEqual(caught.exception.code, "UNSUPPORTED_CAPABILITY")
            self.assertEqual(caught.exception.details["supported_targets"], ["mac", "android"])
            self.assertEqual(platforms.effective_target("android", config())[0], "android")

    def test_ios_and_unknown_tokens_are_reported_differently(self):
        with self.assertRaises(ScaffoldError) as ios:
            platforms.effective_target("ios", config())
        self.assertEqual(ios.exception.code, "UNSUPPORTED_CAPABILITY")
        with self.assertRaises(ScaffoldError) as unknown:
            platforms.effective_target("Debug", config())
        self.assertEqual(unknown.exception.code, "INVALID_INPUT")


class CapabilityTests(unittest.TestCase):
    def test_nothing_is_called_supported_without_recorded_validation(self):
        with mock.patch.object(platforms, "VALIDATED", set()):
            self.assertNotIn("supported", {row["status"] for row in platforms.capability_table()})
        with mock.patch.object(platforms, "VALIDATED", {("mac", "build", "debug", "arm64")}):
            supported = [row for row in platforms.capability_table() if row["status"] == "supported"]
            self.assertEqual([(r["target"], r["operation"]) for r in supported], [("mac", "build")])

    def test_rows_distinguish_host_target_operation_configuration_and_architecture(self):
        row = platforms.capability_table()[0]
        self.assertEqual(set(row), {"host", "target", "operation", "configuration", "architecture", "status", "note"})
        self.assertTrue(any(r["status"] == "unsupported" and r["target"] == "ios" for r in platforms.capability_table()))


if __name__ == "__main__":
    unittest.main()
