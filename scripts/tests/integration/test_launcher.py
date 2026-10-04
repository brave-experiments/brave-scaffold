# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Launcher behavior: runtime resolution, recovery messages, environment independence."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.support import SCRIPTS


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="scaffold launcher ")))
        cls.installation = cls.root / "install with spaces"
        shutil.copytree(SCRIPTS, cls.installation / "scripts",
                        ignore=shutil.ignore_patterns(".venv", "__pycache__", "tests"))
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(cls.installation / "scripts" / ".venv")],
                       check=True)
        cls.bdev = cls.installation / "scripts" / "bdev"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def run_bdev(self, *args, env=None, cwd=None, launcher=None):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.root)}
        environment.update(env or {})
        return subprocess.run([str(launcher or self.bdev), *args], cwd=str(cwd or self.root), env=environment,
                              capture_output=True, text=True)

    def test_runs_from_an_unrelated_directory_in_a_path_with_spaces(self):
        result = self.run_bdev("capabilities", "--json", cwd="/")
        self.assertEqual(json.loads(result.stdout)["command"], "capabilities")

    def test_runs_through_a_symlink_on_path(self):
        link_dir = self.root / "links"
        link_dir.mkdir(exist_ok=True)
        link = link_dir / "bdev"
        if not link.exists():
            link.symlink_to(self.bdev)
        result = self.run_bdev("capabilities", "--json", launcher=link)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ignores_path_virtualenv_and_python_variables(self):
        poison = self.root / "poison"
        poison.mkdir(exist_ok=True)
        (poison / "json.py").write_text("raise RuntimeError('inherited PYTHONPATH was used')\n")
        env = {"PYTHONPATH": str(poison), "PYTHONHOME": "/nonexistent", "VIRTUAL_ENV": "/nonexistent",
               "PATH": "/nonexistent"}
        result = self.run_bdev("capabilities", "--json", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")

    def test_support_launcher_uses_its_installation_and_isolated_runtime_through_a_symlink(self):
        (self.installation / "support-repos.toml").write_text(
            '[[repos]]\nname = "example"\nurl = "git@example.invalid:example.git"\nbranch = "main"\n')
        link = self.root / "support-command"
        link.symlink_to(self.installation / "scripts" / "sync-support-repos")
        result = self.run_bdev("--status", launcher=link,
                               env={"PYTHONHOME": "/nonexistent", "PYTHONPATH": "/nonexistent"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "example: missing")
        self.assertIn(str(self.installation / ".bdev" / "logs"), result.stderr)
        self.assertFalse((self.installation / "support").exists())

    def test_missing_runtime_shows_the_recreation_command_and_never_uses_path_python(self):
        bare = self.root / "bare"
        shutil.copytree(self.installation / "scripts", bare / "scripts",
                        ignore=shutil.ignore_patterns(".venv", "__pycache__"))
        result = self.run_bdev("--help", launcher=bare / "scripts" / "bdev",
                               env={"PATH": "%s:/usr/bin:/bin" % Path(sys.executable).parent})
        self.assertEqual(result.returncode, 3)
        self.assertIn("venv --clear --without-pip", result.stderr)
        self.assertIn(str(bare / "scripts" / ".venv"), result.stderr)
        self.assertEqual(result.stdout, "")

    def test_broken_runtime_is_reported_the_same_way(self):
        broken = self.root / "broken"
        shutil.copytree(self.installation / "scripts", broken / "scripts",
                        ignore=shutil.ignore_patterns(".venv", "__pycache__"))
        (broken / "scripts" / ".venv" / "bin").mkdir(parents=True)
        (broken / "scripts" / ".venv" / "bin" / "python").symlink_to("/nonexistent/python3.14")
        result = self.run_bdev("context", launcher=broken / "scripts" / "bdev")
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing or broken", result.stderr)

    def test_too_old_runtime_gets_a_diagnostic_before_application_code_loads(self):
        from scaffold import launch
        old = mock.Mock()
        with mock.patch.object(launch.sys, "version_info", (3, 11, 0, "final", 0)), \
                mock.patch.object(launch.sys, "argv", ["launch.py", "bdev"]), \
                mock.patch.object(launch.sys, "stderr") as stderr:
            self.assertEqual(launch.main(), 3)
        message = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("3.14", message)
        self.assertIn("--without-pip", message)


if __name__ == "__main__":
    unittest.main()
