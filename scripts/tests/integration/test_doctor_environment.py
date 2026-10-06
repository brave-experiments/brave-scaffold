# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Doctor judges checkout readiness in the environment execution uses, not the caller's."""

import json
import unittest

from tests.integration.test_build import SKIP, BuildTestCase
from tests.integration.test_prepared_context import extend_environment
from tests.support import write_executable

NEEDS_VARIABLE = "#!/bin/sh\n[ -n \"$SCAFFOLD_SDK_SELECTED\" ] || exit 1\necho 15.0\n"


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class DoctorEnvironmentTests(BuildTestCase):
    def setUp(self):
        super().setUp()
        write_executable(self.sandbox.bin / "xcrun", NEEDS_VARIABLE)

    def doctor(self, env=None):
        result = self.sandbox.bcore("--json", "--config", self.config, "--checkout", "main", "doctor", "mac",
                                   env=env or self.env())
        return result, json.loads(result.stdout)

    def check(self, document, name):
        return next(check for check in document["checks"] if check["name"] == name)

    def test_a_variable_only_the_approved_environment_exports_satisfies_both_execution_and_doctor(self):
        extend_environment(self, "export SCAFFOLD_SDK_SELECTED=1")
        built = self.document("build", "--offline")[0]
        self.assertEqual(built.returncode, 0, built.stderr)
        _, document = self.doctor()
        self.assertEqual(self.check(document, "macos-sdk")["status"], "pass")

    def test_the_callers_variable_does_not_satisfy_doctor_when_the_environment_removes_it(self):
        extend_environment(self, "unset SCAFFOLD_SDK_SELECTED")
        ambient = self.env(SCAFFOLD_SDK_SELECTED="1")
        self.assertEqual(self.document("build", "--offline", env=ambient)[1]["error"]["code"], "READINESS_BLOCKED",
                         "execution loads the approved environment, which lacks the variable")
        result, document = self.doctor(ambient)
        self.assertEqual(self.check(document, "macos-sdk")["status"], "blocker")

    def test_a_failed_environment_load_leaves_checkout_readiness_unchecked_not_passed(self):
        extend_environment(self, "exit 1")
        result, document = self.doctor(self.env(SCAFFOLD_SDK_SELECTED="1"))
        self.assertEqual(self.check(document, "environment")["status"], "blocker")
        sdk = [check for check in document["checks"] if check["name"].startswith("readiness:") or
               check["name"] == "macos-sdk"]
        self.assertTrue(sdk and all(check["status"] == "not_checked" for check in sdk), sdk)

    def test_machine_checks_still_run_without_a_selected_checkout(self):
        self.sandbox.write_config([])
        result = self.sandbox.bcore("--json", "--config", self.config, "doctor", "mac", env=self.env(),
                                   cwd=self.sandbox.root)
        document = json.loads(result.stdout)
        self.assertEqual(self.check(document, "git")["status"], "pass")
        self.assertEqual(self.check(document, "checkout-selection")["status"], "not_checked")


if __name__ == "__main__":
    unittest.main()
