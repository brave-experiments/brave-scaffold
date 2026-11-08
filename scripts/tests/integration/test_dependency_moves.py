# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Sync moves aside dependency checkouts that Chromium replaces with its own files."""

import json
import os
import subprocess
import unittest
from pathlib import Path

from tests.integration.test_build import SKIP, BuildTestCase
from tests.integration.test_sync_scope import git

TAG = "158.0.1.2"
DEPENDENCY = "third_party/lib/src"
CHECKOUT_HOOK = '''
if "sync" in argv:
    import subprocess
    src = os.path.dirname(os.environ["BRAVE_CORE_DIR"])
    %s
    raise SystemExit(subprocess.run(["git", "-C", src, "checkout", "-q", "refs/tags/%s"]).returncode)
'''


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class DependencyMoveTests(BuildTestCase):
    def setUp(self):
        super().setUp()
        upstream = self.sandbox.root / "upstream-lib"
        upstream.mkdir()
        git(upstream, "init", "-q", "-b", "main")
        (upstream / "lib.c").write_text("dependency\n")
        git(upstream, "add", "-A")
        git(upstream, "commit", "-q", "-m", "dependency")
        self.dependency = self.src / DEPENDENCY
        subprocess.run(["git", "clone", "-q", str(upstream), str(self.dependency)], check=True)
        self.pinned = git(self.dependency, "rev-parse", "HEAD").stdout.strip()
        git(self.dependency, "checkout", "-q", "--detach", self.pinned)
        git(self.src, "update-index", "--add", "--cacheinfo", "160000,%s,%s" % (self.pinned, DEPENDENCY))
        git(self.src, "commit", "-q", "-m", "Chromium 157")
        self.target = self.commit_with_files_in_place_of_dependency()
        package_path = self.core / "package.json"
        package = json.loads(package_path.read_text())
        package["config"] = {"projects": {"chrome": {"tag": TAG}}}
        package_path.write_text(json.dumps(package))
        self.hook = self.sandbox.hook(CHECKOUT_HOOK % ("pass", TAG))

    def commit_with_files_in_place_of_dependency(self):
        index = self.sandbox.root / "target-index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}

        def plumbing(*args, stdin=None):
            return subprocess.run(["git", "-C", str(self.src), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                                   *args], env=env, input=stdin, check=True, capture_output=True,
                                  text=True).stdout.strip()

        plumbing("read-tree", "HEAD")
        plumbing("rm", "-q", "--cached", DEPENDENCY)
        blob = plumbing("hash-object", "-w", "--stdin", stdin="vendored by Chromium\n")
        plumbing("update-index", "--add", "--cacheinfo", "100644,%s,%s/lib.c" % (blob, DEPENDENCY))
        commit = plumbing("commit-tree", plumbing("write-tree"), "-p", "HEAD", "-m", "Chromium 158")
        git(self.src, "tag", TAG, commit)
        return commit

    def test_unchanged_dependency_is_moved_aside_and_sync_succeeds(self):
        result, document = self.document("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.src, "rev-parse", "HEAD").stdout.strip(), self.target)
        self.assertEqual((self.dependency / "lib.c").read_text(), "vendored by Chromium\n")
        [moved] = document["data"]["sync"]["moved_dependencies"]
        self.assertEqual((moved["path"], moved["pinned"]), (DEPENDENCY, self.pinned))
        kept = Path(moved["moved_to"])
        self.assertTrue(kept.is_relative_to(self.sandbox.config.parent / ".bcore" / "moved-dependencies"))
        self.assertEqual((kept / "lib.c").read_text(), "dependency\n")
        self.assertEqual(git(kept, "rev-parse", "HEAD").stdout.strip(), self.pinned)
        self.assertEqual(len(self.node_calls()), 1)
        self.assertIn("Moved %s" % DEPENDENCY, result.stderr)

    def test_dependency_with_local_work_stops_before_sync(self):
        cases = {"has local changes": lambda: (self.dependency / "lib.c").write_text("local edit\n"),
                 "has untracked": lambda: (self.dependency / "notes.txt").write_text("notes\n"),
                 "is at": lambda: git(self.dependency, "commit", "-q", "--allow-empty", "-m", "local")}
        for problem, make_work in cases.items():
            with self.subTest(problem=problem):
                git(self.dependency, "reset", "-q", "--hard", self.pinned)
                git(self.dependency, "clean", "-qfd")
                make_work()
                expected = "has local changes" if problem == "has untracked" else problem
                result, document = self.document("sync")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
                self.assertIn(DEPENDENCY + " " + expected, document["error"]["message"])
                self.assertEqual(self.node_calls(), [])
                self.assertTrue((self.dependency / ".git").exists())
                self.assertNotEqual(git(self.src, "rev-parse", "HEAD").stdout.strip(), self.target)

    def test_tag_fetched_during_sync_moves_dependency_and_retries_once(self):
        git(self.src, "tag", "-d", TAG)
        self.hook = self.sandbox.hook(CHECKOUT_HOOK % (
            'subprocess.run(["git", "-C", src, "tag", "-f", "%s", "%s"], check=True, capture_output=True)' % (
                TAG, self.target), TAG))
        result, document = self.document("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.node_calls()), 2)
        self.assertEqual(git(self.src, "rev-parse", "HEAD").stdout.strip(), self.target)
        self.assertEqual(document["data"]["sync"]["moved_dependencies"][0]["path"], DEPENDENCY)

    def test_failure_unrelated_to_dependencies_is_not_retried(self):
        self.hook = self.sandbox.hook('if "sync" in argv:\n    raise SystemExit(4)\n')
        result, document = self.document("sync")
        self.assertEqual(document["error"]["code"], "CHILD_FAILED")
        self.assertEqual(len(self.node_calls()), 1)
        self.assertFalse(self.dependency.exists())

    def test_plan_lists_the_move_without_moving(self):
        result, document = self.document("sync", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        step = document["data"]["plan"]["steps"][-1]
        self.assertEqual(step["status"], "planned")
        self.assertIn(DEPENDENCY, step["detail"])
        self.assertIn(str(self.dependency), step["writes"])
        self.assertTrue((self.dependency / ".git").exists())
        self.assertEqual(self.node_calls(), [])

    def test_plan_is_blocked_by_dependency_with_local_work(self):
        (self.dependency / "lib.c").write_text("local edit\n")
        result, document = self.document("sync", "--plan")
        step = document["data"]["plan"]["steps"][-1]
        self.assertEqual(step["status"], "blocked")
        self.assertIn(DEPENDENCY + " has local changes", step["detail"])

    def test_checkout_already_at_the_tag_moves_nothing(self):
        git(self.src, "checkout", "-q", "--force", self.target)
        result, document = self.document("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("moved_dependencies", document["data"]["sync"])
        self.assertFalse((self.sandbox.config.parent / ".bcore" / "moved-dependencies").exists())


if __name__ == "__main__":
    unittest.main()
