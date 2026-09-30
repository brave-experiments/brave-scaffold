# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Help and delimiters are recognised inside each command's own parsing boundary."""

import json
import shutil
import sys
import unittest

from tests.support import SandboxTest

SKIP = not (shutil.which("direnv") and sys.platform == "darwin")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class ForwardingBoundaryTests(SandboxTest):
    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout("main", git=True)
        self.sandbox.prepare_environment("main")
        self.config = str(self.sandbox.config)
        self.selectors = ("--config", self.config, "--checkout", "main")

    def bdev(self, *args, tool="bdev"):
        return self.sandbox.bdev(*args, tool=tool)

    def child_calls(self, tool):
        return [record for record in self.sandbox.records() if record["tool"] == tool]

    def test_help_after_the_first_program_argument_reaches_the_program(self):
        result = self.bdev("vpython3", *self.selectors, "example.py", "--help", "-h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Usage:", result.stdout)
        (call,) = self.child_calls("vpython3")
        self.assertEqual(call["argv"], ["example.py", "--help", "-h"])

    def test_help_among_the_leading_scaffold_options_is_scaffold_help(self):
        for args in (("vpython3", "--help"), ("vpython3", *self.selectors, "--help", "example.py")):
            with self.subTest(args=args):
                result = self.bdev(*args)
                self.assertIn("Usage: bdev vpython3", result.stdout)
        self.assertEqual(self.sandbox.records(), [])

    def test_help_after_the_delimiter_reaches_the_program(self):
        self.bdev("vpython3", *self.selectors, "--", "--help")
        (call,) = self.child_calls("vpython3")
        self.assertEqual(call["argv"], ["--help"])

    def test_bpm_help_follows_the_same_boundary(self):
        result = self.bdev(*self.selectors, "run", "test", "--help", tool="bpm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Usage:", result.stdout)
        self.assertEqual(self.child_calls("node")[-1]["argv"][1:], ["run", "test", "--help"])
        for args in (("--help",), (*self.selectors, "--help", "run")):
            with self.subTest(args=args):
                self.assertIn("Usage: bpm", self.bdev(*args, tool="bpm").stdout)
        self.sandbox.record.unlink()
        self.bdev(*self.selectors, "--", "--help", tool="bpm")
        self.assertEqual(self.child_calls("node")[-1]["argv"][1:], ["--help"])

    def test_commands_that_do_not_forward_reject_arguments_after_the_delimiter(self):
        for args in (("--json", "capabilities", "--", "unexpected"), ("--json", "context", *self.selectors, "--", "x"),
                     ("--json", "doctor", "shell", "--", "x"), ("--json", "clean", *self.selectors, "--", "--execute")):
            with self.subTest(args=args):
                result = self.bdev(*args)
                document = json.loads(result.stdout)
                self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
                self.assertIn("--", document["error"]["message"])

    def test_an_empty_delimiter_is_harmless_and_forwarding_commands_still_forward(self):
        result = self.bdev("--json", "capabilities", "--")
        self.assertEqual(json.loads(result.stdout)["status"], "ok")
        result = self.bdev("--json", "patches", "update", *self.selectors, "--", "--flag")
        self.assertEqual(self.child_calls("node")[-1]["argv"][-1], "--flag", result.stdout)


if __name__ == "__main__":
    unittest.main()
