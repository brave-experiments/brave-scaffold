# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Large files in the support working copy must be real content, fetched only by explicit setup."""

import json
import shutil
import subprocess
import unittest

from tests.android_fixtures import make_support_repo
from tests.integration.test_android import AndroidTestCase
from tests.integration.test_build import SKIP

LFS = subprocess.run(["git", "lfs", "version"], capture_output=True).returncode == 0
CONTENT = b"large-file-content-" * 100


@unittest.skipIf(SKIP or not LFS, "needs direnv on macOS and git-lfs")
class SupportLargeFileTests(AndroidTestCase):
    def setUp(self):
        super().setUp()
        self.support = make_support_repo(self.sandbox.root / "lfs-source", {"v154": 154, "v155": 155}, lfs=True)
        self.url = "file://" + str(self.support)

    def large_file(self):
        return self.wc() / "res" / "jdk" / "current" / "large.bin"

    def git_lfs(self, *args):
        return subprocess.run(["git", "-C", str(self.wc()), "lfs", *args], capture_output=True, text=True)

    def pointers(self):
        return [line for line in self.git_lfs("ls-files").stdout.splitlines() if " - " in line]

    def leave_a_pointer(self):
        """A working copy whose large file is only a pointer and whose local store holds nothing."""
        self.assertEqual(self.setup_support(source=self.url).returncode, 0)
        pointer = subprocess.run(["git", "-C", str(self.wc()), "show", "HEAD:res/jdk/current/large.bin"],
                                 capture_output=True, check=True).stdout
        self.large_file().write_bytes(pointer)
        shutil.rmtree(self.wc() / ".git" / "lfs")
        self.assertEqual(len(self.pointers()), 1)

    def test_content_missing_from_the_local_store_is_fetched_and_verified_during_setup(self):
        result = self.setup_support(source=self.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(subprocess.run(["git", "-C", str(self.wc()), "config", "--local", "lfs.storage"],
                                       capture_output=True, text=True).stdout.strip().endswith(
                                           "brave-android-mac-support/.git/lfs"))
        self.assertEqual(self.large_file().read_bytes(), CONTENT)
        self.assertEqual(self.pointers(), [])

    def test_setup_that_cannot_fetch_the_content_fails_instead_of_reporting_success(self):
        shutil.rmtree(self.support / ".git" / "lfs")
        result = self.setup_support(source=self.url)
        document = json.loads(result.stdout)
        self.assertEqual((result.returncode, document["error"]["code"]), (5, "CHILD_FAILED"))
        self.assertIn("large file", document["error"]["message"])
        self.assertTrue(document["error"]["repairs"])

    def test_setup_repeated_at_the_same_ref_finishes_an_interrupted_materialization(self):
        self.leave_a_pointer()
        result = self.setup_support(source=self.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.large_file().read_bytes(), CONTENT)

    def test_doctor_and_implicit_builds_report_the_problem_without_fetching(self):
        self.leave_a_pointer()
        self.sandbox.configure_rbe("main")
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['android']\n")
        result = self.sandbox.bcore("--json", "--config", self.config, "doctor", "android", "--checkout", "main",
                                   env=self.env())
        checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["android-support-lfs"]["status"], "blocker")
        self.assertEqual(checks["android-support-lfs"]["repairs"][0]["argv"][:3], ["bcore", "android", "setup"])
        result, document = self.document("build", "android")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "DEPENDENCY_INCOMPATIBLE"))
        self.assertIn("large file", document["error"]["message"])
        commands = [" ".join(entry["argv"]) for entry in document["logs"]]
        self.assertFalse([c for c in commands if "lfs pull" in c or "lfs fetch" in c or " fetch" in c], commands)
        self.assertEqual(len(self.pointers()), 1, "nothing was fetched or repaired")


if __name__ == "__main__":
    unittest.main()
