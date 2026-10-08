# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The inherited environment a checkout's tools start from."""

import unittest

import tests.support  # noqa: F401
from scaffold.common import env


class CleanEnvironTests(unittest.TestCase):
    def test_git_repository_selectors_are_not_passed_on_to_the_checkouts_tools(self):
        cleaned = env.clean_environ({"GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/elsewhere", "GIT_INDEX_FILE": "i",
                                     "GIT_SSH_COMMAND": "ssh -i key", "PATH": "/usr/bin", "DIRENV_DIR": "-/x"})
        self.assertEqual(cleaned, {"GIT_SSH_COMMAND": "ssh -i key", "PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
