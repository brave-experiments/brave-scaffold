# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Core version writes use the same inventory for plans and local-work guards."""

import unittest

from tests.integration.test_build import BUILD_HOOK, SKIP, BuildTestCase
from tests.integration.test_sync_scope import git


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class VersionWriteTests(BuildTestCase):
    def setUp(self):
        super().setUp()
        self.version = self.src / "chrome" / "VERSION"
        self.version.parent.mkdir(exist_ok=True)
        self.version.write_text("upstream version\n")
        self.sidecar = self.version.with_name("VERSION.chromium")
        self.sandbox.commit_all("main")
        self.hook = self.sandbox.hook('''
if "apply_patches" in argv:
    chrome = os.path.join(os.path.dirname(os.environ["BRAVE_CORE_DIR"]), "chrome")
    open(os.path.join(chrome, "VERSION"), "w").write("generated version\\n")
    open(os.path.join(chrome, "VERSION.chromium"), "w").write("upstream version\\n")
''' + BUILD_HOOK)

    def require_preparation(self):
        rewrite = self.core / "rewrite"
        rewrite.mkdir(exist_ok=True)
        (rewrite / "input.txt").write_text("changed input\n")
        self.sandbox.record.unlink(missing_ok=True)

    def assert_preserved(self, path):
        before = path.read_bytes()
        result, document = self.document("build", "--offline")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertIn(str(path.relative_to(self.src)), str(document["error"]["details"]["files"]))
        self.assertEqual(self.node_calls(), [])
        self.assertEqual(path.read_bytes(), before)

    def test_pristine_version_and_absent_sidecar_are_planned_and_written(self):
        self.require_preparation()
        result, document = self.document("build", "--plan", "--offline")
        step = next(s for s in document["data"]["plan"]["steps"] if s["name"] == "patch-preparation")
        self.assertIn(str(self.version), step["writes"])
        self.assertIn(str(self.sidecar), step["writes"])
        result, _ = self.document("build", "--offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version.read_text(), "generated version\n")
        self.assertEqual(self.sidecar.read_text(), "upstream version\n")

    def test_unstaged_version_edit_is_preserved(self):
        self.require_preparation()
        self.version.write_text("wanted edit\n")
        self.assert_preserved(self.version)

    def test_staged_version_with_pristine_worktree_is_preserved(self):
        self.require_preparation()
        self.version.write_text("wanted staged edit\n")
        git(self.src, "add", "chrome/VERSION")
        self.version.write_text("upstream version\n")
        self.assert_preserved(self.version)
        self.assertEqual(git(self.src, "show", ":chrome/VERSION").stdout, "wanted staged edit\n")

    def test_untracked_version_is_unknown(self):
        git(self.src, "rm", "--cached", "chrome/VERSION")
        git(self.src, "commit", "-q", "-m", "version is untracked")
        self.require_preparation()
        self.assert_preserved(self.version)

    def test_existing_unknown_sidecar_is_preserved_even_if_ignored(self):
        self.require_preparation()
        self.sidecar.write_text("wanted sidecar\n")
        with (self.src / ".git" / "info" / "exclude").open("a") as stream:
            stream.write("/chrome/VERSION.chromium\n")
        self.assert_preserved(self.sidecar)

    def generate(self):
        self.require_preparation()
        result, _ = self.document("build", "--offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.sandbox.record.unlink()

    def test_known_generated_versions_can_be_written_again(self):
        self.generate()
        result, _ = self.document("build", "--offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version.read_text(), "generated version\n")
        self.assertEqual(self.sidecar.read_text(), "upstream version\n")

    def test_edit_on_known_generated_sidecar_is_preserved(self):
        self.generate()
        self.sidecar.write_text("wanted changed sidecar\n")
        self.assert_preserved(self.sidecar)

    def test_edit_on_known_generated_version_is_preserved(self):
        self.generate()
        self.version.write_text("wanted changed version\n")
        self.assert_preserved(self.version)

    def test_staged_sidecar_with_known_worktree_is_preserved(self):
        self.generate()
        generated = self.sidecar.read_text()
        self.sidecar.write_text("wanted staged sidecar\n")
        git(self.src, "add", "chrome/VERSION.chromium")
        self.sidecar.write_text(generated)
        self.assert_preserved(self.sidecar)
        self.assertEqual(git(self.src, "show", ":chrome/VERSION.chromium").stdout, "wanted staged sidecar\n")
