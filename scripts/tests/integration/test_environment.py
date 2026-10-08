# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Environment generation, approval, and explicit loading with a real direnv."""

import os
import shutil
import unittest

from tests.support import SandboxTest, SCRIPTS, tree_snapshot, write_executable


@unittest.skipUnless(shutil.which("direnv"), "direnv is required")
class EnvironmentTests(SandboxTest):
    def test_shell_reports_startup_and_exit_failures(self):
        self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        shell = self.sandbox.root / "test-shell"
        for code in (127, 7, 0):
            with self.subTest(code=code):
                if code != 127:
                    write_executable(shell, "#!/bin/sh\nexit %d\n" % code)
                result, document = self.sandbox.bcore_json(
                    "shell", "--checkout", "main", "--config", str(self.sandbox.config),
                    env=self.sandbox.env(SHELL=str(shell)))
                self.assertEqual(document["child_exit_code"], code)
                self.assertEqual(result.returncode, 5 if code else 0)
                self.assertEqual(document["status"], "error" if code else "ok")
                if code:
                    self.assertEqual(document["error"]["code"], "CHILD_FAILED")

    def test_init_writes_outside_core_and_never_approves(self):
        core = self.sandbox.make_checkout("main")
        self.sandbox.register("main")
        before = tree_snapshot(core.parent.parent.parent)
        result = self.sandbox.bcore("env", "init", "--checkout", "main", "--config", str(self.sandbox.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, tree_snapshot(core.parent.parent.parent), "checkout files changed")
        envrc = self.sandbox.root / "config" / "environments" / "main" / ".envrc"
        self.assertTrue(envrc.is_file())
        self.assertIn("direnv allow", result.stdout)
        # Not approved: the next execution stops with an approval repair.
        bpm = self.sandbox.bcore("--json", "--checkout", "main", "--config", str(self.sandbox.config),
                                "run", tool="bpm")
        document = __import__("json").loads(bpm.stdout)
        self.assertEqual(document["error"]["code"], "ENVIRONMENT_UNAPPROVED")
        self.assertEqual(bpm.returncode, 3)
        repair = document["error"]["repairs"][0]
        self.assertEqual(repair["argv"][:2], ["direnv", "allow"])
        self.assertTrue(repair["requires_user_action"])

    def test_approved_environment_runs_in_a_fresh_process_without_hooks(self):
        self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        result = self.sandbox.bcore("env", "check", "--checkout", "main", "--config", str(self.sandbox.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("matches", result.stdout)

    def test_editing_the_environment_invalidates_approval(self):
        self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        envrc = self.sandbox.root / "config" / "environments" / "main" / ".envrc"
        envrc.write_text(envrc.read_text() + "\n# edited\n")
        result, document = self.sandbox.bcore_json("env", "check", "--checkout", "main",
                                                  "--config", str(self.sandbox.config))
        self.assertEqual(document["error"]["code"], "ENVIRONMENT_UNAPPROVED")

    def test_failing_environment_stops_before_any_tool(self):
        self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main", approve=False)
        envrc = self.sandbox.root / "config" / "environments" / "main" / ".envrc"
        envrc.write_text("exit 7\n")
        self.sandbox.approve("main")
        result = self.sandbox.bcore("--json", "--checkout", "main", "--config", str(self.sandbox.config), "run",
                                   tool="bpm")
        document = __import__("json").loads(result.stdout)
        self.assertEqual(document["error"]["code"], "ENVIRONMENT_LOAD_FAILED")
        self.assertEqual(self.sandbox.records(), [])

    def test_a_failing_environment_does_not_expose_inherited_secrets_in_its_error(self):
        self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main", approve=False)
        envrc = self.sandbox.root / "config" / "environments" / "main" / ".envrc"
        envrc.write_text('echo "loading with $API_TOKEN and ${DB_PASSWORD} for https://u:pw-in-url-99@host/x" >&2\n'
                         'echo "visible $HARMLESS_SETTING" >&2\nexit 7\n')
        self.sandbox.approve("main")
        secrets = ("tok-supersecret-123456", "hunter2-long-secret")
        env = self.sandbox.env(API_TOKEN=secrets[0], DB_PASSWORD=secrets[1], HARMLESS_SETTING="plainvalue")
        result = self.sandbox.bcore("--json", "--checkout", "main", "--config", str(self.sandbox.config), "run",
                                   tool="bpm", env=env)
        document = __import__("json").loads(result.stdout)
        self.assertEqual(document["error"]["code"], "ENVIRONMENT_LOAD_FAILED")
        logs = "".join(path.read_text() for path in (self.sandbox.root / "config" / ".bcore" / "logs").glob("*.log"))
        for where, text in (("json", result.stdout), ("console", result.stderr), ("diagnostic log", logs)):
            for secret in (*secrets, "pw-in-url-99"):
                self.assertNotIn(secret, text, "%s leaked in the %s" % (secret, where))
        self.assertIn("visible plainvalue", document["error"]["details"]["stderr"], "ordinary output is kept")
        self.assertIn("loading with ***", document["error"]["details"]["stderr"])

    def test_environment_selecting_another_checkout_conflicts(self):
        self.sandbox.make_checkout("main")
        other = self.sandbox.make_checkout("other")
        self.sandbox.prepare_environment("main", approve=False)
        envrc = self.sandbox.root / "config" / "environments" / "main" / ".envrc"
        envrc.write_text('export BRAVE_CORE_DIR="%s"\n' % other)
        self.sandbox.approve("main")
        result, document = self.sandbox.bcore_json("env", "check", "--checkout", "main",
                                                  "--config", str(self.sandbox.config))
        self.assertEqual(document["error"]["code"], "CHECKOUT_ENV_CONFLICT")
        names = [item["variable"] for item in document["error"]["details"]["mismatches"]]
        self.assertIn("BRAVE_CORE_DIR", names)
        self.assertEqual(self.sandbox.records(), [])

    def test_environment_directory_inside_core_is_rejected(self):
        core = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", core, str(core / "env"))])
        result, document = self.sandbox.bcore_json("env", "init", "--checkout", "main",
                                                  "--config", str(self.sandbox.config))
        self.assertEqual(document["error"]["code"], "INVALID_INPUT")
        self.assertFalse((core / "env").exists())

    def test_existing_custom_environment_file_is_preserved(self):
        self.sandbox.make_checkout("main")
        self.sandbox.register("main")
        directory = self.sandbox.root / "config" / "environments" / "main"
        directory.mkdir(parents=True)
        (directory / ".envrc").write_text("export CUSTOM=1\n")
        result = self.sandbox.bcore("env", "init", "--checkout", "main", "--config", str(self.sandbox.config))
        self.assertEqual(result.returncode, 0)
        self.assertEqual((directory / ".envrc").read_text(), "export CUSTOM=1\n")
        self.assertIn("preserved", result.stdout)

    def test_linked_generated_environment_is_not_claimed_to_be_user_authored(self):
        self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main", approve=False)
        directory = self.sandbox.root / "config" / "environments" / "main"
        target = directory / "saved.envrc"
        envrc = directory / ".envrc"
        envrc.rename(target)
        before = target.read_text()
        envrc.symlink_to(target)
        result = self.sandbox.bcore("env", "init", "--checkout", "main", "--config", str(self.sandbox.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(envrc.is_symlink())
        self.assertEqual(target.read_text(), before)
        self.assertIn("symlink", result.stdout)
        self.assertNotIn("not generated", result.stdout)

    def test_stale_selector_exports_do_not_override_the_selection(self):
        main = self.sandbox.make_checkout("main")
        other = self.sandbox.make_checkout("other")
        self.sandbox.prepare_environment("main")
        env = self.sandbox.env(BRAVE_CORE_DIR=str(other), BRAVE_SRC_ROOT=str(other.parent),
                               BRAVE_LAUNCHER_CHECKOUT_DIR=str(other))
        result = self.sandbox.bcore("env", "check", "--checkout", "main", "--config", str(self.sandbox.config),
                                   env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.sandbox.bcore("--config", str(self.sandbox.config), "run", "x", tool="bpm", env=env,
                                   cwd=main)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.sandbox.records()[-1]
        self.assertEqual(record["cwd"], str(main))
        self.assertEqual(record["launcher"], str(main))

    def test_env_export_is_pure(self):
        core = self.sandbox.make_checkout("main")
        self.sandbox.register("main")
        result = self.sandbox.bcore("env", "export", "--checkout", "main", "--format", "bash",
                                   "--config", str(self.sandbox.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("export BRAVE_CORE_DIR=%s" % core, result.stdout)
        self.assertIn("vendor/depot_tools", result.stdout)
        self.assertNotIn("export PYTHONPATH", result.stdout)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
