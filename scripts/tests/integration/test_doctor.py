# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Doctor scopes, aggregation, and read-only behavior."""

import json
import shutil
import sys
import unittest

from tests.support import SandboxTest, tree_snapshot, write_executable

MAC_ARM64 = sys.platform == "darwin"


@unittest.skipUnless(shutil.which("direnv") and MAC_ARM64, "needs direnv on a macOS host")
class DoctorTests(SandboxTest):
    def setUp(self):
        super().setUp()
        write_executable(self.sandbox.bin / "xcode-select", "#!/bin/sh\necho /fake/Xcode/Developer\n")
        self.core = self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        self.config = str(self.sandbox.config)

    def doctor(self, *args, cwd=None, env=None):
        result = self.sandbox.bcore("--json", "--config", self.config, "doctor", *args, cwd=cwd, env=env)
        return result, json.loads(result.stdout)

    def statuses(self, document):
        return {check["name"]: check["status"] for check in document["checks"]}

    def test_combined_scopes_report_shared_checks_once(self):
        result, document = self.doctor("--checkout", "main")
        names = [check["name"] for check in document["checks"]]
        self.assertEqual(len(names), len(set(names)))
        rbe = next(check for check in document["checks"] if check["name"] == "rbe-env")
        self.assertTrue(rbe["required"])
        self.assertEqual(rbe["scopes"], ["mac", "rbe"])
        repairs = document["error"]["repairs"]
        self.assertEqual(len(repairs), len({json.dumps(step, sort_keys=True) for step in repairs}))

    def test_default_scopes_leave_android_ios_and_signing_opt_in(self):
        result, document = self.doctor("--checkout", "main")
        self.assertEqual(document["data"]["scopes"], ["mac", "rbe", "shell"])
        names = {check["name"] for check in document["checks"]}
        for name in ("adb", "android-gclient-target", "git-signing-format", "signer-program", "signing-key"):
            self.assertNotIn(name, names)
        for scope in ("android", "ios", "signing"):
            _, named = self.doctor(scope, "--checkout", "main")
            self.assertEqual(named["data"]["scopes"], [scope])

    def two_checkouts(self):
        second = self.sandbox.make_checkout("second")
        self.sandbox.prepare_environment("second")
        self.sandbox.write_config([("main", self.core, "environments/main"),
                                   ("second", second, "environments/second")])
        return second

    def test_outside_checkout_checks_all_and_keeps_failures_separate(self):
        second = self.two_checkouts()
        (self.core / "third_party/node/node-mac-arm64/bin/node").unlink()
        before = tree_snapshot(second.parents[3])
        result, document = self.doctor("mac")
        self.assertEqual(result.returncode, 3)
        reports = document["data"]["checkouts"]
        self.assertEqual([(r["alias"], r["status"]) for r in reports], [("main", "error"), ("second", "ok")])
        self.assertEqual(sum(c["name"] == "scaffold-runtime" for c in document["checks"]), 1)
        self.assertIn("main/local-tools", document["error"]["details"]["blocking"])
        self.assertIsNone(document["context"]["checkout"])
        self.assertEqual(before, tree_snapshot(second.parents[3]))

    def test_matching_host_checks_appear_once_in_text_but_remain_in_json(self):
        self.two_checkouts()
        result = self.sandbox.bcore("--config", self.config, "doctor", "mac")
        self.assertEqual(result.stdout.count("Host platform:"), 1)
        self.assertEqual(result.stdout.count("macOS SDK:"), 1)
        self.assertEqual(result.stdout.count("Free disk space"), 1)
        self.assertEqual(result.stdout.count("Brave services key"), 2)
        _, document = self.doctor("mac")
        for report in document["data"]["checkouts"]:
            self.assertIn("macos-sdk", [check["name"] for check in report["checks"]])

    def test_explicit_and_cwd_selection_limit_inspection(self):
        second = self.two_checkouts()
        for args, cwd in [(("mac", "--checkout", "second"), self.sandbox.root), (("mac",), second)]:
            result, document = self.doctor(*args, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("checkouts", document["data"])
            self.assertEqual(document["context"]["checkout"], str(second))

    def test_all_required_checks_pass(self):
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual((result.returncode, document["status"], document["error"]), (0, "ok", None))
        self.assertEqual(document["data"]["scopes"], ["mac"])
        self.assertIsNone(document["child_exit_code"])
        required = {c["status"] for c in document["checks"] if c["required"]}
        self.assertEqual(required, {"pass"})

    def test_optional_warnings_do_not_fail_readiness_but_are_retained(self):
        result, document = self.doctor("shell")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"))
        codes = {warning["code"] for warning in document["warnings"]}
        self.assertIn("CHECK_WARNING", codes)
        self.assertEqual(self.statuses(document)["bcore-on-path"], "warning")

    def test_required_blocker_fails_with_the_check_named(self):
        node = self.core / "third_party" / "node" / "node-mac-arm64" / "bin" / "node"
        node.unlink()
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
        self.assertEqual(self.statuses(document)["local-tools"], "blocker")
        self.assertIn("local-tools", document["error"]["details"]["blocking"])
        local = next(c for c in document["checks"] if c["name"] == "local-tools")
        self.assertTrue(local["repairs"])

    def test_unchecked_required_checks_are_incomplete_but_machine_evidence_remains(self):
        self.sandbox.write_config([])
        result, document = self.doctor("mac", cwd=self.sandbox.root)
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_INCOMPLETE"))
        statuses = self.statuses(document)
        self.assertEqual(statuses["git"], "pass")
        self.assertEqual(statuses["checkout-selection"], "not_checked")
        self.assertEqual(statuses["environment"], "not_checked")
        selection = next(c for c in document["checks"] if c["name"] == "checkout-selection")
        self.assertIn("--checkout", json.dumps(selection["evidence"]))

    def test_blocker_takes_precedence_over_unchecked(self):
        self.sandbox.write_config([])
        env = self.sandbox.env(PATH=str(self.sandbox.bin) + ":/nonexistent")
        result, document = self.doctor("mac", cwd=self.sandbox.root, env=env)
        self.assertEqual(document["error"]["code"], "READINESS_BLOCKED")
        self.assertIn("checkout-selection", document["error"]["details"]["incomplete"])

    def test_unapproved_environment_blocks_and_asks_the_user(self):
        envrc = self.sandbox.root / "config" / "environments" / "main" / ".envrc"
        envrc.write_text(envrc.read_text() + "# changed\n")
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual(self.statuses(document)["environment"], "blocker")
        repairs = next(c for c in document["checks"] if c["name"] == "environment")["repairs"]
        self.assertTrue(repairs[0]["requires_user_action"])

    def test_unknown_and_deferred_scopes_are_input_errors(self):
        result, document = self.doctor("nonsense")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))
        self.assertIn("mac", document["error"]["details"]["scopes"])
        result, document = self.doctor("android-studio")
        self.assertEqual((result.returncode, document["error"]["code"]), (2, "UNSUPPORTED_CAPABILITY"))

    def test_doctor_writes_only_external_diagnostics_and_never_repairs(self):
        self.sandbox.mark_stale("main")
        before_checkout = tree_snapshot(self.core.parents[3])
        before_config = tree_snapshot(self.sandbox.root / "config")
        self.doctor("mac", "--checkout", "main")
        self.assertEqual(before_checkout, tree_snapshot(self.core.parents[3]))
        after_config = tree_snapshot(self.sandbox.root / "config")
        logs = self.sandbox.root / "config" / ".bcore" / "logs"
        allowed = {str(logs.parent), str(logs), *(str(path) for path in logs.glob("*.log"))}
        self.assertEqual(before_config, {path: after_config.get(path) for path in before_config})
        self.assertLessEqual(after_config.keys() - before_config.keys(), allowed)
        self.assertTrue(list(logs.glob("*.log")))
        self.assertEqual([r for r in self.sandbox.records() if r["tool"] == "installer"], [])

    def test_text_mode_matches_the_json_verdict(self):
        self.sandbox.write_config([])
        result = self.sandbox.bcore("--config", self.config, "doctor", "mac", cwd=self.sandbox.root)
        self.assertEqual(result.returncode, 3)
        self.assertIn("Readiness incomplete:", result.stdout)
        self.assertIn("❔  Checkout checks need a selected checkout", result.stdout)


if __name__ == "__main__":
    unittest.main()
