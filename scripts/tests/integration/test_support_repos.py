# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Support commands exercise real local Git repositories without credentials or direnv."""

import os
import signal
import shutil
import time
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS
from scaffold.common.config import toml_string


class SupportRepositoriesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for name in list(self.env):
            if name.startswith("GIT_") and name not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
                del self.env[name]
        self.source = self.root / "source"
        self.git(self.root, "init", "-b", "main", str(self.source))
        self.commit(self.source, "first")
        shutil.copytree(SCRIPTS, self.root / "scripts", ignore=shutil.ignore_patterns(".venv", "tests", "__pycache__"))
        (self.root / "scripts" / ".venv").symlink_to(SCRIPTS / ".venv", target_is_directory=True)
        self.launcher = self.root / "scripts" / "sync-support-repos"
        self.directory = self.root / "support"
        self.manifest = self.root / "support-repos.toml"
        self.write_manifest()

    def git(self, directory, *arguments):
        result = subprocess.run(["git", "-c", "user.name=Test User", "-c", "user.email=test@example.com",
                                 "-c", "commit.gpgsign=false", *arguments], cwd=directory, env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, directory, text):
        (directory / "tracked").write_text(text)
        self.git(directory, "add", "tracked")
        self.git(directory, "commit", "-m", text)
        return self.git(directory, "rev-parse", "HEAD")

    def write_manifest(self, entries=None):
        entries = entries if entries is not None else [("handbook", str(self.source), "main")]
        text = "repos = []\n" if not entries else ""
        for name, url, branch in entries:
            text += '\n[[repos]]\n' + ''.join('%s = %s\n' % (key, toml_string(value)) for key, value in
                                             zip(("name", "url", "branch"), (name, url, branch)))
        self.manifest.write_text(text)

    def command(self, *args, expected=0, env=None, cwd=None):
        result = subprocess.run([str(self.launcher), *args], cwd=cwd or self.root,
                                env=env or self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return result

    def checkout(self):
        self.command()
        return self.directory / "handbook"

    def test_status_does_not_clone_and_sync_clones_then_fast_forwards_with_logged_commands(self):
        status = self.command("--status")
        self.assertIn("handbook: missing", status.stdout)
        self.assertFalse(self.directory.exists())
        checkout = self.checkout()
        before = self.git(checkout, "rev-parse", "HEAD")
        after = self.commit(self.source, "upstream")
        self.assertNotEqual(before, after)
        document = self.command()
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), after)
        self.assertIn("handbook: updated", document.stdout)
        self.assertIn("git fetch", document.stderr)
        log = next((self.root / ".bcore" / "logs").glob("*.log"))
        self.assertTrue(log.is_file())

    def test_dirty_files_and_local_commits_survive_normal_sync_and_explicit_discard_resets(self):
        checkout = self.checkout()
        (checkout / "tracked").write_text("local edit")
        self.commit(self.source, "remote change")
        self.command(expected=6)
        self.assertEqual((checkout / "tracked").read_text(), "local edit")
        self.commit(checkout, "local commit")
        before = self.git(checkout, "rev-parse", "HEAD")
        document = self.command(expected=6)
        self.assertIn("handbook: skipped", document.stdout)
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), before)
        (checkout / "untracked").write_text("keep")
        document = self.command("--discard-local", "--execute")
        self.assertIn("handbook: reset", document.stdout)
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), self.git(self.source, "rev-parse", "HEAD"))
        self.assertEqual((checkout / "untracked").read_text(), "keep")

    def test_fast_forward_refuses_to_overwrite_an_ignored_local_file(self):
        checkout = self.checkout()
        (checkout / ".git" / "info" / "exclude").write_text("new-file\n")
        (checkout / "new-file").write_text("local content")
        (self.source / "new-file").write_text("remote content")
        self.git(self.source, "add", "new-file")
        self.git(self.source, "commit", "-m", "track new file")
        before = self.git(checkout, "rev-parse", "HEAD")
        self.command(expected=5)
        self.assertEqual((checkout / "new-file").read_text(), "local content")
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), before)

    def test_wrong_branch_and_origin_are_skipped_even_when_discard_is_requested(self):
        checkout = self.checkout()
        self.git(checkout, "switch", "-c", "work")
        self.command("--discard-local", "--execute", expected=6)
        self.assertEqual(self.git(checkout, "branch", "--show-current"), "work")
        self.git(checkout, "switch", "main")
        self.git(checkout, "remote", "set-url", "origin", str(self.root / "other"))
        self.command(expected=6)
        self.assertEqual(self.git(checkout, "remote", "get-url", "origin"), str(self.root / "other"))

    def test_url_rewrite_rules_do_not_make_a_matching_origin_look_different(self):
        checkout = self.checkout()
        mirror = self.root / "mirror"
        self.git(self.root, "clone", "--bare", str(self.source), str(mirror))
        self.git(checkout, "config", "url.%s.insteadOf" % mirror, str(self.source))
        self.assertEqual(self.git(checkout, "remote", "get-url", "origin"), str(mirror))
        after = self.commit(self.source, "upstream")
        self.git(mirror, "fetch", str(self.source), "main:main")
        document = self.command()
        self.assertIn("handbook: updated", document.stdout)
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), after)

    def test_prune_preview_and_execute_only_remove_unlisted_clean_repositories(self):
        checkout = self.checkout()
        self.git(self.directory, "clone", str(self.source), "obsolete")
        obsolete = self.directory / "obsolete"
        preview = self.command("--prune")
        self.assertEqual(preview.stdout.strip(), "obsolete: would prune")
        self.assertTrue(obsolete.exists())
        self.command("--prune", "--execute")
        self.assertFalse(obsolete.exists())
        self.assertTrue(checkout.exists())

    def test_prune_preserves_dirty_ignored_stashed_and_unpushed_work(self):
        checkout = self.checkout()
        self.write_manifest([])
        (checkout / "untracked").write_text("keep")
        self.command("--prune", "--execute", expected=6)
        (checkout / "untracked").unlink()
        (checkout / ".git" / "info" / "exclude").write_text("ignored\n")
        (checkout / "ignored").write_text("keep")
        self.command("--prune", "--execute", expected=6)
        (checkout / "ignored").unlink()
        (checkout / "tracked").write_text("stash me")
        self.git(checkout, "stash", "push")
        self.command("--prune", "--execute", expected=6)
        self.git(checkout, "stash", "clear")
        self.git(checkout, "switch", "-c", "saved-work")
        self.commit(checkout, "local commit")
        self.git(checkout, "switch", "main")
        self.command("--prune", "--execute", expected=6)
        self.assertTrue(checkout.exists())

    def test_prune_preserves_no_remote_detached_shallow_and_linked_worktrees(self):
        checkout = self.checkout()
        self.write_manifest([])
        self.git(checkout, "switch", "--detach")
        self.command("--prune", "--execute", expected=6)
        self.git(checkout, "switch", "main")
        linked = self.root / "linked"
        self.git(checkout, "worktree", "add", "-b", "linked", str(linked))
        self.command("--prune", "--execute", expected=6)
        self.assertTrue(linked.exists())
        self.git(checkout, "worktree", "remove", str(linked))
        self.git(checkout, "remote", "remove", "origin")
        self.command("--prune", "--execute", expected=6)
        self.assertTrue(checkout.exists())
        self.git(self.directory, "clone", "--depth=1", self.source.as_uri(), "shallow")
        document = self.command("--prune", "--execute", expected=6)
        self.assertIn("shallow: skipped (Shallow", document.stdout)

    def test_invalid_options_and_manifest_paths_fail_before_repository_mutation(self):
        for args in (("--discard-local",), ("--execute",), ("lock",),
                     ("--status", "--prune"), ("--status", "--discard-local", "--execute"),
                     ("--checkout", "main"), ("--config", "config.toml"), ("--json",), ("--repo", "unknown"), ("--prune", "--repo", "handbook")):
            with self.subTest(args=args):
                self.command(*args, expected=2)
                self.assertFalse(self.directory.exists())
        for name in ("../escape", "/absolute", "a/b", ".git"):
            self.write_manifest([(name, str(self.source), "main")])
            self.command(expected=2)
            self.assertFalse(self.directory.exists())

    def test_symlinks_and_nonrepositories_are_preserved(self):
        self.directory.mkdir()
        target = self.root / "target"
        target.mkdir()
        (target / "precious").write_text("keep")
        destination = self.directory / "handbook"
        destination.symlink_to(target, target_is_directory=True)
        self.command(expected=6)
        self.assertEqual((target / "precious").read_text(), "keep")
        destination.unlink()
        destination.mkdir()
        (destination / "precious").write_text("keep")
        self.command(expected=6)
        self.assertEqual((destination / "precious").read_text(), "keep")

    def test_named_selection_uses_the_installation_despite_cwd_config_and_git_selectors(self):
        self.write_manifest([("handbook", str(self.source), "main"), ("other", str(self.source), "main")])
        (self.root / "brave-scaffold.toml").write_text("this is not valid TOML")
        env = dict(self.env, GIT_DIR=str(self.source / ".git"), GIT_WORK_TREE=str(self.source))
        self.command("--repo", "handbook", env=env, cwd=self.home)
        self.assertTrue((self.directory / "handbook" / ".git").is_dir())
        self.assertFalse((self.directory / "other").exists())
        self.assertEqual(self.git(self.source, "status", "--porcelain"), "")

    def test_cancelled_clone_reports_cancellation_and_stops_its_children(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "started"
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\necho started > %s\nsleep 30\n" % marker)
        hook.chmod(0o755)
        env = dict(self.env, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=str(hooks))
        process = subprocess.Popen([str(self.launcher)],
                                   cwd=self.root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "the Git hook must start before cancellation")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, stderr)
            self.assertIn("Interrupted", stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)

    def test_support_folder_cannot_redirect_operations_into_core(self):
        core = self.root / "browser" / "src" / "brave"
        core.mkdir(parents=True)
        (core / "package.json").write_text('{"name": "brave-core"}')
        self.directory.symlink_to(core, target_is_directory=True)
        self.command(expected=4, cwd=core)
        self.assertEqual(sorted(path.name for path in core.iterdir()), ["package.json"])

    def test_invalid_branch_and_duplicate_names_fail_before_clone(self):
        self.write_manifest([("handbook", str(self.source), "bad..branch")])
        self.command(expected=2)
        self.assertFalse(self.directory.exists())
        self.write_manifest([("handbook", str(self.source), "main"),
                             ("handbook", str(self.source), "main")])
        self.command(expected=2)
        self.assertFalse(self.directory.exists())

    def test_child_failure_reports_completed_repositories_and_never_claims_success(self):
        self.write_manifest([("handbook", str(self.source), "main"),
                             ("missing", str(self.root / "absent"), "main")])
        document = self.command(expected=5)
        self.assertIn("Git failed", document.stderr)
        self.assertIn("handbook: cloned", document.stdout)
        self.assertTrue((self.directory / "handbook").exists())


if __name__ == "__main__":
    unittest.main()
