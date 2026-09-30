# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Secrets passed to package commands never appear in results, errors, plans, or saved records."""

import json
import signal
import subprocess
import time
import unittest

from tests.integration.test_build import SKIP, BuildTestCase
from tests.support import SCRIPTS

SECRET = "s3cr3t-value-xyz"
SECRET_ARGS = ["--token=" + SECRET, "--password", SECRET + "-2", "https://user:%s-3@example.com/path" % SECRET]


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class SecretRedactionTests(BuildTestCase):
    def saved_records(self):
        directory = self.sandbox.config.parent / ".bdev"
        return {str(path): path.read_text() for path in directory.rglob("*") if path.is_file()}

    def assert_secret_free(self, *outputs):
        for text in outputs:
            self.assertNotIn(SECRET, text)
        for path, text in self.saved_records().items():
            self.assertNotIn(SECRET, text, path)

    def child_received_secret(self):
        return any("--token=" + SECRET in call["argv"] for call in self.node_calls())

    def bdev_text(self, *args, env=None):
        return self.sandbox.bdev("--config", self.config, "--checkout", "main", *args, env=env or self.env())

    def test_successful_json_results_and_records_hide_secrets_but_the_child_receives_them(self):
        for command in (["build", *SECRET_ARGS], ["test", "brave_unit_tests", *SECRET_ARGS],
                        ["sync", *SECRET_ARGS], ["patches", "update", *SECRET_ARGS]):
            with self.subTest(command=command):
                self.sandbox.record.unlink(missing_ok=True)
                result = self.bdev(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.child_received_secret(), "execution keeps the real arguments")
                self.assertTrue(any("--token=***" in entry["argv"] for entry in json.loads(result.stdout)["logs"]))
                self.assert_secret_free(result.stdout, result.stderr)

    def test_failed_children_hide_secrets_in_json_and_text(self):
        env = self.env(FAKE_EXIT="7")
        for command in (["build", *SECRET_ARGS], ["test", "brave_unit_tests", *SECRET_ARGS], ["sync", *SECRET_ARGS]):
            with self.subTest(command=command):
                result = self.bdev(*command, env=env)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], "CHILD_FAILED")
                self.assert_secret_free(result.stdout, result.stderr)
                text = self.bdev_text(*command, env=env)
                self.assertNotEqual(text.returncode, 0)
                self.assert_secret_free(text.stdout, text.stderr)

    def test_plans_hide_secrets(self):
        for command in (["build", "--plan", *SECRET_ARGS], ["sync", "--plan", *SECRET_ARGS],
                        ["test", "brave_unit_tests", "--plan", *SECRET_ARGS]):
            with self.subTest(command=command):
                self.assert_secret_free(self.bdev(*command).stdout, self.bdev_text(*command).stdout)

    def test_direct_package_results_hide_secrets(self):
        for env, flags in ((self.env(), ["--json"]), (self.env(FAKE_EXIT="3"), ["--json"]), (self.env(FAKE_EXIT="3"), [])):
            result = self.sandbox.bdev(*flags, "--config", self.config, "--checkout", "main", "run", "x", *SECRET_ARGS,
                                       tool="bpm", env=env)
            self.assert_secret_free(result.stdout, result.stderr)

    def test_a_cancelled_build_leaves_no_secret_in_its_result_or_record(self):
        process = subprocess.Popen(
            [str(SCRIPTS / "bdev"), "--json", "--config", self.config, "--checkout", "main", "build", *SECRET_ARGS],
            env=self.env(FAKE_SLEEP="60"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not [r for r in self.node_calls() if "build" in r["argv"]] and time.time() < deadline:
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(json.loads(stdout)["status"], "cancelled")
        self.assert_secret_free(stdout, stderr)


if __name__ == "__main__":
    unittest.main()
