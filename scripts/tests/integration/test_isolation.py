# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Independence from Core, competing tools and commands, and switching between checkouts."""

import json
import os
import shutil
import unittest
from pathlib import Path

from tests.support import SandboxTest, tree_snapshot, write_executable

SKIP = shutil.which("direnv") is None


class NotificationIsolationTests(SandboxTest):
    """Tests never raise a real desktop notification or ring the terminal, whatever process they start."""

    def test_the_sandbox_environment_turns_off_notification_delivery(self):
        from scaffold.common import notify
        environment = self.sandbox.env()
        self.assertEqual(environment.get(notify.BACKEND_VARIABLE), "none")
        self.assertIsNone(notify.default_notifier(environment))
        self.assertIsNone(notify.default_bell(environment))

    def test_a_test_can_still_ask_for_a_notification_backend_explicitly(self):
        from scaffold.common import notify
        self.assertIsNotNone(notify.default_notifier(self.sandbox.env(**{notify.BACKEND_VARIABLE: "macos"})))


@unittest.skipIf(SKIP, "direnv is required")
class IsolationTests(SandboxTest):
    def test_scaffold_commands_leave_core_and_its_git_state_untouched(self):
        core = self.sandbox.make_checkout("main", git=True)
        self.sandbox.commit_all("main")
        config = str(self.sandbox.config)
        before = tree_snapshot(core.parents[3])
        for args in (["setup", "--config", config], ["checkout", "add", "main", str(core), "--config", config],
                     ["env", "init", "--checkout", "main", "--config", config]):
            self.assertEqual(self.sandbox.bcore(*args).returncode, 0, args)
        self.sandbox.approve("main")
        for args in (["context"], ["doctor", "mac"], ["env", "check"], ["env", "export", "--format", "bash"],
                     ["drift"], ["clean"], ["build", "--plan"], ["sync", "--plan"], ["capabilities"]):
            result = self.sandbox.bcore(*args, "--checkout", "main", "--config", config)
            self.assertNotEqual(result.returncode, 1, (args, result.stderr))
        self.assertEqual(before, tree_snapshot(core.parents[3]), "no file, config, hook, or exclude changed in Core")

    def test_switching_between_checkouts_uses_each_ones_own_tools(self):
        first = self.sandbox.make_checkout("first")
        second = self.sandbox.make_checkout("second")
        self.sandbox.write_config([("first", first, "environments/first"), ("second", second, "environments/second")])
        for name in ("first", "second"):
            self.assertEqual(self.sandbox.bcore("env", "init", "--checkout", name, "--config",
                                               str(self.sandbox.config)).returncode, 0)
            self.sandbox.approve(name)
        for core in (first, second, first):
            nested = core / "components"
            nested.mkdir(exist_ok=True)
            result = self.sandbox.bcore("--config", str(self.sandbox.config), "run", "x", cwd=nested, tool="bpm")
            self.assertEqual(result.returncode, 0, result.stderr)
            record = self.sandbox.records()[-1]
            self.assertEqual(record["cwd"], str(core))
            self.assertTrue(record["argv"][0].startswith(str(core)), "manager entry is inside the selected checkout")
            self.assertEqual(record["launcher"], str(core))

    def test_environment_that_adds_another_checkouts_tools_cannot_redirect_execution(self):
        first = self.sandbox.make_checkout("first")
        second = self.sandbox.make_checkout("second")
        self.sandbox.write_config([("first", first, "environments/first")])
        self.sandbox.bcore("env", "init", "--checkout", "first", "--config", str(self.sandbox.config))
        envrc = self.sandbox.root / "config" / "environments" / "first" / ".envrc"
        other_node = second / "third_party" / "node" / "node-mac-arm64" / "bin"
        envrc.write_text(envrc.read_text() + 'export PATH="%s:$PATH"\nexport BRAVE_LAUNCHER_CHECKOUT_DIR="%s"\n' %
                         (other_node, second))
        self.sandbox.approve("first")
        result = self.sandbox.bcore("--json", "--config", str(self.sandbox.config), "--checkout", "first", "run", "x",
                                   tool="bpm")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "CHECKOUT_ENV_CONFLICT")
        self.assertEqual(self.sandbox.records(), [])
        envrc.write_text(envrc.read_text().replace('export BRAVE_LAUNCHER_CHECKOUT_DIR="%s"\n' % second, ""))
        self.sandbox.approve("first")
        result = self.sandbox.bcore("--config", str(self.sandbox.config), "--checkout", "first", "run", "x", tool="bpm")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.sandbox.records()[-1]
        node_bin = first / "third_party" / "node" / "node-mac-arm64" / "bin"
        self.assertEqual(record["path"].split(os.pathsep)[1], str(node_bin), "own tools stay first")
        self.assertTrue(record["argv"][0].startswith(str(first)))

    def test_a_competing_bcore_on_path_does_not_redirect_generated_environments(self):
        core = self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        write_executable(self.sandbox.bin / "bcore", "#!/bin/sh\necho competing >&2\nexit 9\n")
        result = self.sandbox.bcore("env", "check", "--checkout", "main", "--config", str(self.sandbox.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("competing", result.stderr)

    def test_configuration_edits_never_reach_environment_files_unasked(self):
        core = self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        envrc = self.sandbox.root / "config" / "environments" / "main" / ".envrc"
        before = envrc.read_text()
        self.sandbox.bcore("checkout", "add", "main", str(core), "--config", str(self.sandbox.config))
        self.assertEqual(envrc.read_text(), before)


if __name__ == "__main__":
    unittest.main()
