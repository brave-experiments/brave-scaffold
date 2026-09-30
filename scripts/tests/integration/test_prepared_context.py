# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Every execution phase uses the approved environment that was loaded and validated for the checkout."""

import json
import shutil
import unittest

from tests.integration.test_android import DEVICES_TWO, AndroidTestCase
from tests.integration.test_build import SKIP, BuildTestCase
from tests.support import write_executable


def extend_environment(test, *lines):
    """Add exports to the checkout's approved environment and approve the new contents (disposable sandbox)."""
    envrc = test.sandbox.root / "config" / "environments" / "main" / ".envrc"
    envrc.write_text(envrc.read_text() + "\n" + "\n".join(lines) + "\n")
    test.sandbox.approve("main")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class MacRunEnvironmentTests(BuildTestCase):
    def test_run_stops_before_any_restart_when_the_environment_names_another_checkout(self):
        self.assertEqual(self.document("build")[0].returncode, 0)
        extend_environment(self, "export BRAVE_CORE_DIR=/somewhere/else")
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.document("run")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "CHECKOUT_ENV_CONFLICT"))
        self.assertEqual(self.sandbox.records(), [], "nothing was stopped or launched")

    def test_run_still_restarts_with_a_valid_environment(self):
        self.assertEqual(self.document("build")[0].returncode, 0)
        result, document = self.document("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue([r for r in self.sandbox.records() if r["tool"] == "open"])


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class AndroidEnvironmentTests(AndroidTestCase):
    def setUp(self):
        super().setUp()
        self.setup_support()

    def android(self, command, *args, **extra):
        env = self.env(FAKE_ADB_DEVICES="emulator-5554,device", **extra)
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", command, "android",
                                   *args, env=env)
        return result, json.loads(result.stdout)

    def test_run_stops_before_the_device_when_the_environment_names_another_checkout(self):
        self.assertEqual(self.android("build")[0].returncode, 0)
        extend_environment(self, "export BRAVE_CORE_DIR=/somewhere/else")
        self.sandbox.record.unlink(missing_ok=True)
        result, document = self.android("run")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "CHECKOUT_ENV_CONFLICT"))
        self.assertEqual(self.adb_calls(), [])

    def test_adb_comes_from_the_approved_environment(self):
        sdk = self.sandbox.root / "sdk"
        write_executable(sdk / "platform-tools" / "adb", (self.sandbox.bin / "adb").read_text())
        (self.sandbox.bin / "adb").unlink()
        self.assertEqual(self.android("build")[0].returncode, 0)
        result, document = self.android("run")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "LOCAL_TOOL_MISSING"))
        extend_environment(self, "export ANDROID_HOME=%s" % sdk)
        result, document = self.android("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(document["data"]["run"]["device"], "emulator-5554")

    def test_build_settings_exported_by_the_environment_reach_the_build(self):
        extend_environment(self, "export SCAFFOLD_ANDROID_SISO_LOCAL_JOBS=3", "export JAVA_OPTS=-Xmx2G")
        hook = self.sandbox.hook(open(self.hook).read().split("\n", 1)[1] + """
import json
open(os.environ["FAKE_ENV_DUMP"], "w").write(json.dumps({key: os.environ.get(key) for key in ("SISO_LIMITS", "JAVA_OPTS")}))
""")
        dump = self.sandbox.root / "env-dump.json"
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "build", "android",
                                   env=self.sandbox.env(FAKE_HOOK=hook, FAKE_ENV_DUMP=str(dump)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(dump.read_text()), {"SISO_LIMITS": "local=3", "JAVA_OPTS": "-Xmx2G"})


if __name__ == "__main__":
    unittest.main()
