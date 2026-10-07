# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The arguments Core's sync command receives."""

import unittest
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import sync
from scaffold.common.results import ScaffoldError

IDENTITY = SimpleNamespace(workspace="/workspace")


class SyncArgumentsTests(unittest.TestCase):
    def arguments(self, target, forwarded, existing=()):
        with mock.patch.object(sync, "gclient_targets", return_value=list(existing)):
            return sync.sync_arguments(None, target, forwarded, IDENTITY)

    def test_desktop_arguments_pass_through_unchanged_and_in_order(self):
        self.assertEqual(self.arguments("mac", ["--force", "-C", "false", "--target_os=android"]),
                         ["run", "sync", "--force", "-C", "false", "--target_os=android"])

    def test_mobile_targets_add_the_union_and_pass_other_arguments_on(self):
        self.assertEqual(self.arguments("android", ["--fetch_all"], existing=["ios"]),
                         ["run", "sync", "--target_os=android,ios", "--fetch_all"])

    def test_mobile_targets_refuse_arguments_that_would_change_the_target_list(self):
        for forwarded in (["--target_os=ios"], ["--target_os", "ios"]):
            with self.subTest(forwarded=forwarded), self.assertRaises(ScaffoldError) as caught:
                self.arguments("android", forwarded)
            self.assertEqual(caught.exception.code, "SELECTOR_CONFLICT")
        with self.assertRaises(ScaffoldError) as caught:
            self.arguments("ios", ["--nohooks"])
        self.assertEqual(caught.exception.code, "SELECTOR_CONFLICT")


if __name__ == "__main__":
    unittest.main()
