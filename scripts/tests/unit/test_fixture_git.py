# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fixture Git commands do not leave background work running."""

import os
import subprocess
import unittest

import tests.support as support


class FixtureGitTests(unittest.TestCase):
    def config(self, key):
        return subprocess.run(["git", "config", "--get", key], capture_output=True, text=True).stdout.strip()

    def test_automatic_maintenance_and_gc_are_off_for_every_test_process(self):
        self.assertEqual((self.config("maintenance.auto"), self.config("gc.auto")), ("false", "0"))

    def test_settings_are_added_after_any_that_were_already_present(self):
        environ = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "x"}
        support.quiet_git_maintenance(environ)
        self.assertEqual(environ["GIT_CONFIG_COUNT"], "3")
        self.assertEqual((environ["GIT_CONFIG_KEY_0"], environ["GIT_CONFIG_VALUE_0"]), ("user.name", "x"))
        self.assertEqual((environ["GIT_CONFIG_KEY_1"], environ["GIT_CONFIG_KEY_2"]), ("maintenance.auto", "gc.auto"))


if __name__ == "__main__":
    unittest.main()
