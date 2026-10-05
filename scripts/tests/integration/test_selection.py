# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checkout selection, aliases, and unsupported Git layouts."""

import os
import shutil
import shlex
import stat
import subprocess
import unittest
from pathlib import Path

from tests.support import SandboxTest, SCRIPTS, write_executable


def context(sandbox, *args, cwd=None, env=None):
    result, document = sandbox.bdev_json("context", "--config", str(sandbox.config), *args, cwd=cwd, env=env)
    return result, document


class SelectionTests(SandboxTest):
    def test_cd_prints_only_the_selected_path_and_rejects_unknown_alias(self):
        main = self.sandbox.make_checkout("main")
        other = self.sandbox.make_checkout("other")
        self.sandbox.write_config([("main", main, None), ("alt-1", other, None)])
        for alias, expected in [("main", main), ("alt-1", other)]:
            result = self.sandbox.bdev("cd", alias, "--config", str(self.sandbox.config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(expected) + "\n")
            self.assertEqual(result.stderr, "")
        result, document = self.sandbox.bdev_json("cd", "missing", "--config", str(self.sandbox.config))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(document["error"]["code"], "CHECKOUT_NOT_FOUND")

    def test_cd_shell_function_changes_directory_and_failure_keeps_cwd(self):
        main = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", main, None)])
        # Use the real launcher with an isolated config; no user shell startup files.
        env = self.sandbox.env()
        env["PATH"] = str(SCRIPTS) + os.pathsep + env["PATH"]
        env["BDEV_TEST_CONFIG"] = str(self.sandbox.config)
        script = 'source "$1"; bdev cd main || exit; pwd -P; if bdev cd missing >/dev/null 2>&1; then exit 1; fi; pwd -P'
        # The launcher reads its default config; a tiny PATH wrapper selects this fixture.
        wrapper = self.sandbox.root / "bin"
        wrapper.mkdir(exist_ok=True)
        write_executable(wrapper / "bdev", '#!/bin/sh\nexec ' + shlex.quote(str(SCRIPTS / "bdev")) + ' --config "$BDEV_TEST_CONFIG" "$@"\n')
        env["PATH"] = str(wrapper) + os.pathsep + env["PATH"]
        for shell in ("bash", "zsh"):
            result = subprocess.run([shell, "-f", "-c", script, shell, str(SCRIPTS / "bdev-shell.sh")],
                                    cwd=self.sandbox.root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [str(main), str(main)])
            self.assertEqual(result.stderr, "cd " + str(main) + "\n")

    def test_cd_shell_function_accepts_notification_options(self):
        main = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", main, None)])
        env = self.sandbox.env()
        env["BDEV_TEST_CONFIG"] = str(self.sandbox.config)
        wrapper = self.sandbox.root / "bin"
        wrapper.mkdir(exist_ok=True)
        write_executable(wrapper / "bdev", '#!/bin/sh\nexec ' + shlex.quote(str(SCRIPTS / "bdev")) + ' --config "$BDEV_TEST_CONFIG" "$@"\n')
        env["PATH"] = str(wrapper) + os.pathsep + env["PATH"]
        for shell in ("bash", "zsh"):
            for arguments in ("main --notify=never", "--notify main", "--notify=major main --notify=major"):
                with self.subTest(shell=shell, arguments=arguments):
                    script = 'source "$1"; bdev cd %s || exit; pwd -P' % arguments
                    result = subprocess.run([shell, "-f", "-c", script, shell, str(SCRIPTS / "bdev-shell.sh")],
                                            cwd=self.sandbox.root, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), [str(main)])

    def test_explicit_selector_beats_cwd_and_needs_no_alias(self):
        main = self.sandbox.make_checkout("main")
        other = self.sandbox.make_checkout("other")
        self.sandbox.write_config([(None, main, None)])
        result, document = context(self.sandbox, "--checkout", str(other), cwd=main)
        self.assertEqual(document["context"]["checkout"], str(other))
        self.assertEqual(document["context"]["selection_source"], "explicit_path")

    def test_cwd_beats_stale_inherited_exports(self):
        main = self.sandbox.make_checkout("main")
        other = self.sandbox.make_checkout("other")
        self.sandbox.write_config([("main", main, None), ("other", other, None)])
        env = self.sandbox.env(BRAVE_CORE_DIR=str(other), BRAVE_SRC_ROOT=str(other.parent),
                               BRAVE_BROWSER_DIR=str(other.parents[3]))
        nested = main / "components"
        nested.mkdir()
        result, document = context(self.sandbox, cwd=nested, env=env)
        self.assertEqual(document["context"]["checkout"], str(main))
        self.assertEqual(document["context"]["selection_source"], "cwd")

    def test_alias_and_symlinked_path_resolve_to_one_identity(self):
        main = self.sandbox.make_checkout("main")
        link = self.sandbox.root / "link-to-main"
        link.symlink_to(main)
        self.sandbox.write_config([("main", main, None)])
        _, by_alias = context(self.sandbox, "--checkout", "main")
        _, by_link = context(self.sandbox, "--checkout", str(link))
        self.assertEqual(by_alias["context"]["checkout"], by_link["context"]["checkout"])
        self.assertEqual(by_link["context"]["alias"], "main")

    def test_outer_and_chromium_paths_select_core(self):
        main = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", main, None)])
        for selector in (main.parents[3], main.parent):
            _, document = context(self.sandbox, "--checkout", str(selector))
            self.assertEqual(document["context"]["checkout"], str(main))

    def test_ambiguous_outer_workspace_requires_a_precise_path(self):
        main = self.sandbox.make_checkout("main")
        second = main.parents[2] / "second" / "src" / "brave"
        shutil.copytree(main, second, symlinks=True)
        self.sandbox.write_config([])
        result, document = context(self.sandbox, "--checkout", str(main.parents[3]))
        self.assertEqual(document["error"]["code"], "CHECKOUT_AMBIGUOUS")
        self.assertEqual(len(document["error"]["details"]["candidates"]), 2)
        self.assertEqual(result.returncode, 2)

    def test_outside_a_checkout_requires_an_explicit_selection(self):
        main = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", main, None)])
        result, document = self.sandbox.bdev_json("env", "check", "--config", str(self.sandbox.config))
        self.assertEqual(document["error"]["code"], "CHECKOUT_REQUIRED")
        self.assertEqual(document["error"]["details"]["candidates"], ["main"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("--checkout", document["error"]["details"]["example"])

    def test_relative_selector_resolves_from_the_callers_directory(self):
        main = self.sandbox.make_checkout("main")
        self.sandbox.write_config([])
        _, document = context(self.sandbox, "--checkout", "./brave-main", cwd=self.sandbox.root)
        self.assertEqual(document["context"]["checkout"], str(main))

    def test_unknown_alias_lists_candidates(self):
        main = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", main, None)])
        result, document = context(self.sandbox, "--checkout", "nope")
        self.assertEqual(document["error"]["code"], "CHECKOUT_NOT_FOUND")
        self.assertEqual(document["error"]["details"]["candidates"], ["main"])

    def test_conflicting_duplicate_selectors_fail(self):
        main = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", main, None)])
        result, document = context(self.sandbox, "--checkout", "main", "--checkout", str(main.parent))
        self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT")
        _, same = context(self.sandbox, "--checkout", "main", "--checkout=main")
        self.assertEqual(same["status"], "ok")


class LinkedWorktreeTests(SandboxTest):
    """Linked worktrees are rejected before any environment or child execution."""

    def make_linked(self, repo):
        common = self.sandbox.root / ("common-" + repo.name + "-" + repo.parent.name)
        gitdir = common / "worktrees" / "w"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        shutil.rmtree(repo / ".git")
        (repo / ".git").write_text("gitdir: %s\n" % gitdir)

    def fake_direnv(self):
        """A direnv that records any use, to prove none happened."""
        write_executable(self.sandbox.bin / "direnv",
                         "#!/bin/sh\necho invoked >> '%s'\nexit 1\n" % (self.sandbox.root / "direnv-used"))

    def test_linked_core_and_chromium_are_rejected_early(self):
        for role, pick in (("core", lambda core: core), ("chromium", lambda core: core.parent)):
            with self.subTest(role=role):
                sandbox = self.sandbox
                core = sandbox.make_checkout("wt-" + role)
                sandbox.write_config([("wt", core, "environments/wt")])
                self.make_linked(pick(core))
                self.fake_direnv()
                for tool, args in (("bpm", ["--checkout", "wt", "run"]),
                                   ("bdev", ["vpython3", "--checkout", "wt", "--", "x.py"]),
                                   ("bdev", ["env", "init", "--checkout", "wt"]),
                                   ("bdev", ["tools", "setup", "--checkout", "wt"])):
                    result, document = sandbox.bdev_json("--config", str(sandbox.config), *args, tool=tool)
                    self.assertEqual(document["error"]["code"], "UNSUPPORTED_CAPABILITY", (tool, args))
                    self.assertIn(role, [item["role"] for item in document["error"]["details"]["worktrees"]])
                self.assertFalse((sandbox.root / "direnv-used").exists())
                self.assertEqual(sandbox.records(), [])

    def test_linked_outer_repository_is_rejected(self):
        core = self.sandbox.make_checkout("outer")
        outer = core.parents[3]
        (outer / ".git").mkdir()
        self.make_linked(outer)
        self.sandbox.write_config([("outer", core, "environments/outer")])
        result, document = self.sandbox.bdev_json("--config", str(self.sandbox.config), "env", "init",
                                                  "--checkout", "outer")
        self.assertEqual(document["error"]["code"], "UNSUPPORTED_CAPABILITY")

    def test_read_only_context_explains_the_unsupported_layout(self):
        core = self.sandbox.make_checkout("wt")
        self.sandbox.write_config([("wt", core, "environments/wt")])
        self.make_linked(core)
        self.fake_direnv()
        result, document = context(self.sandbox, "--checkout", "wt")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(document["data"]["layout"]["supported"])
        self.assertEqual(document["warnings"][0]["code"], "UNSUPPORTED_CAPABILITY")
        self.assertFalse((self.sandbox.root / "direnv-used").exists())

    def test_ordinary_layouts_are_not_mistaken_for_worktrees(self):
        # A detached HEAD, a .git file pointing at a separate git directory, and a
        # repository that itself has linked worktrees elsewhere are all ordinary.
        core = self.sandbox.make_checkout("plain")
        (core / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
        separate = self.sandbox.root / "separate-git-dir"
        separate.mkdir()
        shutil.rmtree(core.parent / ".git")
        (core.parent / ".git").write_text("gitdir: %s\n" % separate)
        (core / ".git" / "worktrees" / "elsewhere").mkdir(parents=True)
        self.sandbox.write_config([("plain", core, None)])
        result, document = context(self.sandbox, "--checkout", "plain")
        self.assertEqual(document["status"], "ok")
        self.assertTrue(document["data"]["layout"]["supported"])
        self.assertEqual(document["warnings"], [])


if __name__ == "__main__":
    unittest.main()
