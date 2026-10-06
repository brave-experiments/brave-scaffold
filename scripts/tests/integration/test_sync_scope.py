# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Sync delegates source changes and failures to Core's package command."""

import json
import subprocess
import unittest
from tests.integration.test_build import SKIP, BuildTestCase


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class SyncDispatchTests(BuildTestCase):
    def test_core_receives_local_work_and_changed_sync_sources_without_preparation(self):
        target = self.src / "base" / "BUILD.gn"
        target.write_text("staged work\n")
        git(self.src, "add", "base/BUILD.gn")
        target.write_text("working edit\n")
        dependency = self.sandbox.add_dependency("main", "v8")
        (dependency / "test.cc").write_text("dependency edit\n")
        (self.core / "notes.txt").write_text("untracked Core work\n")
        (self.core / "pnpm-workspace.yaml").write_text("allowBuilds: {}\n")
        package_path = self.core / "package.json"
        package = json.loads(package_path.read_text())
        package["scripts"] = {"sync": "node custom-sync.js", "presync": "node custom-presync.js"}
        package_path.write_text(json.dumps(package))
        self.hook = self.sandbox.hook('''
if "sync" in argv:
    import subprocess
    from pathlib import Path
    core = Path(os.environ["BRAVE_CORE_DIR"])
    src = core.parent
    assert (src / "base/BUILD.gn").read_text() == "working edit\\n"
    assert subprocess.check_output(["git", "-C", str(src), "show", ":base/BUILD.gn"], text=True) == "staged work\\n"
    assert (src / "v8/test.cc").read_text() == "dependency edit\\n"
    assert (core / "notes.txt").read_text() == "untracked Core work\\n"
    (src / "base/BUILD.gn").write_text("Core sync output\\n")
''')
        result, document = self.document("sync", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", "--force"])
        self.assertEqual(len(self.node_calls()), 1)
        self.assertEqual(target.read_text(), "Core sync output\n")
        self.assertNotIn("scope", document["data"]["sync"])
        self.assertNotIn("overwrite_backup", document["data"]["sync"])
        state = self.sandbox.config.parent / ".bcore"
        self.assertFalse(list(state.rglob("sync-baseline.json")))
        self.assertFalse((state / "backups").exists())

    def test_sync_forwards_core_options_including_dependency_deletion(self):
        arguments = ["--force", "--nohooks", "--sync_chromium=false", "-D",
                     "--delete_unused_deps", "--delete_unversioned_trees", "--custom-option=value"]
        result, document = self.document("sync", *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", *arguments])
        self.assertEqual(document["data"]["sync"]["argv"][2:], ["run", "sync", *arguments])

    def test_plan_shows_dispatch_without_reading_sync_sources_or_changing_work(self):
        target = self.src / "base" / "BUILD.gn"
        target.write_text("local work\n")
        (self.src.parent / ".gclient_entries").write_text("not a dependency inventory\n")
        result, document = self.document("sync", "--force", "-D", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        step = document["data"]["plan"]["steps"][-1]
        self.assertEqual(step["status"], "planned")
        self.assertEqual(step["argv"][2:], ["run", "sync", "--force", "-D"])
        self.assertEqual(step["cwd"], str(self.core))
        self.assertEqual(self.node_calls(), [])
        self.assertEqual(target.read_text(), "local work\n")

    def test_core_failure_keeps_partial_changes_and_stops_combined_commands(self):
        target = self.src / "base" / "BUILD.gn"
        self.hook = self.sandbox.hook('''
if "sync" in argv:
    from pathlib import Path
    (Path(os.environ["BRAVE_CORE_DIR"]).parent / "base/BUILD.gn").write_text("partial sync\\n")
    print("Core sync failed after writing sources", flush=True)
    raise SystemExit(9)
raise AssertionError("no command may run after sync fails")
''')
        for command in ("sync", "sync-build", "sync-build-run"):
            with self.subTest(command=command):
                self.sandbox.record.unlink(missing_ok=True)
                result, document = self.document(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(document["error"]["code"], "CHILD_FAILED")
                self.assertEqual(document["child_exit_code"], 9)
                self.assertEqual([call["argv"][1:] for call in self.node_calls()], [["run", "sync"]])
                self.assertEqual(target.read_text(), "partial sync\n")
                self.assertIn("Core sync failed after writing sources", result.stderr)


if __name__ == "__main__":
    unittest.main()
