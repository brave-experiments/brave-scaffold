# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""macOS build and RBE/Siso readiness checks through the real CLI."""

import json
import shutil
import sys
import unittest

from tests.support import SandboxTest, tree_snapshot, write_executable

SECRET = "sekret-services-value"
CREDENTIAL = "hunter2pw"


@unittest.skipUnless(shutil.which("direnv") and sys.platform == "darwin", "needs direnv on a macOS host")
class RbeCheckTests(SandboxTest):
    def setUp(self):
        super().setUp()
        bin = self.sandbox.bin
        write_executable(bin / "xcode-select", "#!/bin/sh\necho /fake/Developer\n")
        write_executable(bin / "xcrun", "#!/bin/sh\necho 15.0\n")
        write_executable(bin / "openssl", "#!/bin/sh\nexit ${FAKE_OPENSSL_EXIT:-0}\n")
        self.core = self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        self.config = str(self.sandbox.config)
        self.certs = self.sandbox.root / "certs"
        self.certs.mkdir()
        (self.certs / "client.crt").write_text("cert")
        (self.certs / "client.key").write_text("key")
        (self.sandbox.root / "siso-cache").mkdir()

    def write_env(self, **overrides):
        values = {"rbe_service": "https://user:%s@rbe.example:443" % CREDENTIAL, "use_remoteexec": "true",
                  "rbe_tls_client_auth_cert": str(self.certs / "client.crt"),
                  "rbe_tls_client_auth_key": str(self.certs / "client.key"),
                  "siso_cache_dir": str(self.sandbox.root / "siso-cache"), "brave_services_key": SECRET}
        values.update(overrides)
        (self.core / ".env").write_text("".join("%s=%s\n" % (k, v) for k, v in values.items() if v is not None))

    def write_sync_artifacts(self):
        siso = self.core.parent / "build" / "config" / "siso"
        siso.mkdir(parents=True)
        (self.core.parents[1] / ".gclient").write_text("reapi_address https://user:%s@rbe.example:443\n" % CREDENTIAL)
        cache = self.sandbox.root / "siso-cache"
        (siso / ".sisorc").write_text('reapi_keep_exec_stream googlechrome -local_cache_enable -cache_dir "%s"\n' % cache)
        (siso / ".sisoenv").write_text("REAPI=https://user:%s@rbe.example:443\n" % CREDENTIAL)

    def doctor(self, scope, *extra, cwd=None, env=None):
        result = self.sandbox.bdev("--json", "--config", self.config, "doctor", scope, *extra, cwd=cwd, env=env)
        return result, json.loads(result.stdout)

    def status(self, document):
        return {check["name"]: check["status"] for check in document["checks"]}

    def test_configured_rbe_passes_and_reachability_is_declared_untested(self):
        self.write_env()
        self.write_sync_artifacts()
        result, document = self.doctor("rbe", "--checkout", "main")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), document["error"])
        status = self.status(document)
        for name in ("rbe-env", "rbe-tls-files", "rbe-siso-cache", "rbe-gclient", "rbe-sisorc", "rbe-sisoenv"):
            self.assertEqual(status[name], "pass", name)
        self.assertEqual(status["rbe-reachability"], "not_checked")
        reach = next(c for c in document["checks"] if c["name"] == "rbe-reachability")
        self.assertFalse(reach["required"])
        self.assertIn("not tested", reach["summary"])

    def test_missing_rbe_configuration_blocks_the_rbe_scope_but_only_warns_for_mac(self):
        self.write_env(rbe_service=None, use_remoteexec="false")
        result, document = self.doctor("rbe", "--checkout", "main")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
        self.assertEqual(self.status(document)["rbe-env"], "blocker")
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), document["error"])
        env_check = next(c for c in document["checks"] if c["name"] == "rbe-env")
        self.assertEqual((env_check["status"], env_check["required"]), ("warning", False))
        self.assertIn("--offline", env_check["summary"])

    def test_stale_sync_artifacts_and_expiring_certificate_are_reported(self):
        self.write_env()
        result, document = self.doctor("rbe", "--checkout", "main")
        self.assertEqual(self.status(document)["rbe-gclient"], "blocker")
        self.assertIn("bpm", json.dumps(next(c for c in document["checks"] if c["name"] == "rbe-gclient")["repairs"]))
        self.write_sync_artifacts()
        result, document = self.doctor("rbe", "--checkout", "main", env=self.sandbox.env(FAKE_OPENSSL_EXIT="1"))
        self.assertEqual(self.status(document)["rbe-tls-expiry"], "blocker")

    def test_unreadable_certificate_and_missing_cache_block(self):
        self.write_env(rbe_tls_client_auth_cert=str(self.certs / "absent.crt"),
                       siso_cache_dir=str(self.sandbox.root / "no-cache"))
        result, document = self.doctor("rbe", "--checkout", "main")
        status = self.status(document)
        self.assertEqual((status["rbe-tls-files"], status["rbe-siso-cache"]), ("blocker", "blocker"))

    def test_missing_services_key_warns_and_secrets_never_appear(self):
        self.write_env(brave_services_key="")
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual(self.status(document)["services-key"], "warning")
        self.write_env()
        self.write_sync_artifacts()
        json_result, document = self.doctor("mac", "--checkout", "main")
        text = self.sandbox.bdev("--config", self.config, "doctor", "mac", "--checkout", "main")
        self.assertEqual(self.status(document)["services-key"], "pass")
        for output in (json_result.stdout, json_result.stderr, text.stdout, text.stderr):
            self.assertNotIn(SECRET, output)
            self.assertNotIn(CREDENTIAL, output)

    def test_include_files_supply_the_services_key(self):
        (self.core / "shared.env").write_text("brave_services_key=%s\n" % SECRET)
        (self.core / ".env").write_text("include_env=shared.env\n")
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual(self.status(document)["services-key"], "pass")

    def test_no_checkout_keeps_machine_evidence_and_marks_config_unchecked(self):
        result, document = self.doctor("rbe", cwd=self.sandbox.root)
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_INCOMPLETE"))
        unchecked = next(c for c in document["checks"] if c["name"] == "rbe-config")
        self.assertEqual((unchecked["status"], unchecked["required"]), ("not_checked", True))
        result, document = self.doctor("mac", cwd=self.sandbox.root)
        status = self.status(document)
        self.assertEqual((status["macos-sdk"], status["services-key"]), ("pass", "not_checked"))

    def test_missing_sdk_is_a_required_blocker(self):
        write_executable(self.sandbox.bin / "xcrun", "#!/bin/sh\nexit 1\n")
        self.write_env()
        result, document = self.doctor("mac", "--checkout", "main")
        self.assertEqual((result.returncode, self.status(document)["macos-sdk"]), (3, "blocker"))

    def test_text_mode_and_no_writes(self):
        self.write_env()
        before = (tree_snapshot(self.core.parents[3]), tree_snapshot(self.sandbox.root / "config"))
        text = self.sandbox.bdev("--config", self.config, "doctor", "rbe", "--checkout", "main")
        self.assertIn("rbe-reachability", text.stdout)
        self.assertIn("NOT_CHECKED", text.stdout)
        self.assertEqual(before, (tree_snapshot(self.core.parents[3]), tree_snapshot(self.sandbox.root / "config")))


if __name__ == "__main__":
    unittest.main()
