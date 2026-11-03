# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Java runtime check judges the Java that Core's formatter would run on."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import tests.support  # noqa: F401
from scaffold.brave import android_checks


def fake_jdk(root, name, banner):
    home = Path(root) / name
    (home / "bin").mkdir(parents=True)
    java = home / "bin" / "java"
    java.write_text("#!/bin/sh\necho '%s' >&2\n" % banner)
    java.chmod(0o755)
    return home


class JavaRuntimeCheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.identity = SimpleNamespace(src=self.src)

    def check(self, **environ):
        ctx = SimpleNamespace(environ={"PATH": "/nonexistent", **environ}, log=None)
        return android_checks.java_runtime_check(ctx, "android", self.identity)

    def test_major_versions(self):
        self.assertEqual(android_checks.java_major('openjdk version "21.0.3" 2024-04-16'), 21)
        self.assertEqual(android_checks.java_major('openjdk version "25" 2025-09-16'), 25)
        self.assertEqual(android_checks.java_major('java version "1.8.0_402"'), 8)
        self.assertIsNone(android_checks.java_major("not java"))

    def test_java_21_passes(self):
        home = fake_jdk(self.root, "jdk21", 'openjdk version "21.0.3" 2024-04-16')
        check = self.check(JAVA_HOME=str(home))
        self.assertEqual(check.status, "pass")
        self.assertTrue(check.required)

    def test_java_17_blocks_and_names_the_bundled_jdk(self):
        home = fake_jdk(self.root, "jdk17", 'openjdk version "17.0.9" 2023-10-17')
        (self.src / "third_party" / "jdk" / "current").mkdir(parents=True)
        check = self.check(JAVA_HOME=str(home))
        self.assertEqual(check.status, "blocker")
        self.assertTrue(check.required)
        self.assertIn("Java 17", check.summary)
        self.assertIn("third_party/jdk/current", check.summary)

    def test_missing_java_blocks(self):
        check = self.check()
        self.assertEqual(check.status, "blocker")
        self.assertIn("No Java runtime", check.summary)


if __name__ == "__main__":
    unittest.main()
