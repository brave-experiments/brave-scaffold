# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Results of every delivered command conform to the published, versioned schemas."""

import copy
import json
import signal
import subprocess
import time
import unittest

from tests.integration.test_android import AndroidTestCase
from tests.integration.test_build import SKIP, BuildTestCase
from tests.schema_validation import Validator, load
from tests.support import SCRIPTS, SandboxTest, write_executable

VALIDATOR = Validator()


def problems(document):
    return VALIDATOR.problems(document)


class SchemaCase:
    def check(self, document, label=""):
        self.assertEqual(problems(document), [], "%s %s" % (document.get("command"), label))
        return document


class PublishedSchemaTests(unittest.TestCase):
    def test_every_delivered_command_has_a_data_shape(self):
        from scaffold.brave.registry import REGISTRY
        envelope = load("result-envelope.schema.json")
        selected = {rule["if"]["properties"]["command"]["const"]: rule["then"]["properties"]["data"]["$ref"]
                    for rule in envelope["allOf"] if "command" in rule["if"]["properties"]}
        data = load("command-data.schema.json")["$defs"]
        commands = {spec.name for spec in REGISTRY.values()}
        self.assertEqual(sorted(commands - set(selected)), [], "a command without a published data shape")
        for command, reference in selected.items():
            self.assertIn(reference.split("/")[-1], data, command)

    def test_the_validator_rejects_wrong_shapes(self):
        good = {"schema_version": 1, "status": "ok", "command": "clean", "operation_id": None,
                "context": {"checkout": None, "selection_source": None},
                "data": {"mode": "preview", "targets": ["mac"], "configurations": ["debug"], "arch": None,
                         "out_dir": None, "total_kib": None, "entries": []},
                "checks": [], "warnings": [], "error": None, "artifacts": [], "logs": [], "exit_code": 0,
                "child_exit_code": None}
        self.assertEqual(problems(good), [])
        for label, mutate in (("missing data field", lambda d: d["data"].pop("entries")),
                              ("wrong enum", lambda d: d["data"].update(mode="both")),
                              ("ok with an error", lambda d: d.update(error={"code": "X", "message": "m", "details": {},
                                                                              "repairs": []})),
                              ("ok with a failing exit code", lambda d: d.update(exit_code=5)),
                              ("bad artifact", lambda d: d.update(artifacts=[{"path": "x"}])),
                              ("unknown status", lambda d: d.update(status="fine")),
                              ("error without an error", lambda d: d.update(status="error", exit_code=2))):
            broken = copy.deepcopy(good)
            mutate(broken)
            with self.subTest(label):
                self.assertNotEqual(problems(broken), [])


class MachineCommandTests(SchemaCase, SandboxTest):
    def test_setup_registration_context_and_environment_commands(self):
        core = self.sandbox.make_checkout("main")
        config = str(self.sandbox.config)
        self.check(self.sandbox.bcore_json("capabilities")[1])
        self.check(self.sandbox.bcore_json("setup", "--config", config)[1])
        self.check(self.sandbox.bcore_json("checkout", "add", "main", str(core), "--config", config)[1])
        self.check(self.sandbox.bcore_json("checkout", "list", "--config", config)[1])
        self.check(self.sandbox.bcore_json("env", "init", "--checkout", "main", "--config", config)[1])
        self.check(self.sandbox.bcore_json("env", "export", "--checkout", "main", "--config", config)[1])
        self.sandbox.approve("main")
        self.check(self.sandbox.bcore_json("env", "check", "--checkout", "main", "--config", config)[1])
        self.check(self.sandbox.bcore_json("context", "--config", config)[1], "without a checkout")
        self.check(self.sandbox.bcore_json("context", "--checkout", "main", "--config", config)[1])
        self.check(self.sandbox.bcore_json("doctor", "mac", "--checkout", "main", "--config", config)[1], "doctor")
        write_executable(self.sandbox.bin / "true-shell", "#!/bin/sh\nexit 0\n")
        env = self.sandbox.env(SHELL=str(self.sandbox.bin / "true-shell"))
        result = self.sandbox.bcore("--json", "shell", "--checkout", "main", "--config", config, env=env)
        self.check(json.loads(result.stdout))

    def test_errors_and_parse_failures_conform(self):
        for args in (["nonsense"], ["env", "check"], ["doctor", "unknown"], ["context", "--bogus"]):
            result, document = self.sandbox.bcore_json(*args)
            self.check(document, "error")
            self.assertEqual(document["status"], "error")


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class MacResultTests(SchemaCase, BuildTestCase):
    def test_build_family_results_including_unresolved_output_and_errors(self):
        verified = self.check(self.document("build")[1])
        self.assertEqual(verified["data"]["build"]["artifact_status"], "verified")
        unresolved = self.check(self.document("build", "--target=brave_unit_tests")[1], "unresolved")
        self.assertEqual(unresolved["warnings"][0]["code"], "ARTIFACT_UNRESOLVED")
        refused = self.check(self.document("build-run", "--target=brave_unit_tests")[1], "unresolved combined")
        self.assertEqual(refused["error"]["code"], "ARTIFACT_UNRESOLVED")
        failed = self.check(self.document("build", env=self.env(FAKE_EXIT="7"))[1], "child failure")
        self.assertEqual(failed["child_exit_code"], 7)
        self.check(self.document("sync-build")[1], "sync-build")
        self.check(self.document("build-run")[1], "build-run")

    def test_test_sync_run_clean_drift_and_patch_update_results(self):
        self.check(self.document("test", "brave_unit_tests")[1])
        self.check(self.document("sync")[1])
        self.assertEqual(self.document("build")[0].returncode, 0)
        self.check(self.document("run")[1])
        self.check(self.document("drift")[1])
        self.check(self.document("patches", "update")[1])
        self.check(self.document("clean")[1], "preview")
        self.check(self.document("clean", "--execute")[1], "execute")
        self.sandbox.mark_stale("main")
        self.check(self.document("tools", "setup")[1])
        self.check(self.sandbox.bcore_json("doctor", "mac", "--checkout", "main", "--config", self.config,
                                          env=self.env())[1], "doctor")

    def test_plans_conform(self):
        for args in (["build"], ["build-run"], ["sync-build"], ["test", "brave_unit_tests"], ["sync"], ["run"]):
            with self.subTest(args=args):
                if args == ["run"]:
                    self.assertEqual(self.document("build")[0].returncode, 0)
                document = self.check(self.document(*args, "--plan")[1], "plan")
                self.assertTrue(document["data"]["plan"]["steps"])

    def test_direct_tools_conform(self):
        result = self.sandbox.bcore("--json", "--config", self.config, "--checkout", "main", "run", "x", tool="bpm",
                                   env=self.env())
        self.check(json.loads(result.stdout))
        result = self.sandbox.bcore("--json", "--config", self.config, "--checkout", "main", "run", "x", tool="bpm",
                                   env=self.env(FAKE_EXIT="3"))
        self.check(json.loads(result.stdout), "child failure")
        result = self.sandbox.bcore("--json", "vpython3", "--config", self.config, "--checkout", "main", "--", "x.py",
                                   env=self.env())
        self.check(json.loads(result.stdout))

    def test_a_partial_clean_and_a_readiness_error_conform(self):
        (self.src / "out" / "Debug_arm64").mkdir(parents=True, exist_ok=True)
        (self.src / "out" / "Debug_arm64" / ".git").mkdir()
        result, document = self.document("clean", "--execute")
        self.assertEqual(document["status"], "partial")
        self.check(document, "partial")
        self.sandbox.configure_rbe("main", siso_cache_dir=str(self.sandbox.root / "nowhere"))
        self.check(self.document("build")[1], "readiness")

    def test_a_cancelled_operation_conforms(self):
        process = subprocess.Popen(
            [str(SCRIPTS / "bcore"), "--json", "--config", self.config, "--checkout", "main", "build"],
            env=self.env(FAKE_SLEEP="60"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not self.node_calls() and time.time() < deadline:
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=60)
        document = self.check(json.loads(stdout), "cancelled")
        self.assertEqual((document["status"], document["exit_code"]), ("cancelled", 143))


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class AndroidResultTests(SchemaCase, AndroidTestCase):
    def test_android_setup_build_deploy_and_plans_conform(self):
        document = self.check(json.loads(self.setup_support().stdout))
        self.assertEqual(document["status"], "ok")
        self.sandbox.configure_rbe("main")
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['android']\n")
        built = self.check(self.document("build", "android")[1])
        self.assertEqual(built["artifacts"][0]["kind"], "apk")
        env = self.env(FAKE_ADB_DEVICES="emulator-5554,device")
        for args in (["deploy", "android"], ["build-run", "android"], ["run", "android", "--plan"],
                     ["build-run", "android", "--plan"]):
            result = self.sandbox.bcore("--json", "--config", self.config, "--checkout", "main", *args, env=env)
            self.check(json.loads(result.stdout), " ".join(args))
        result = self.sandbox.bcore("--json", "--config", self.config, "--checkout", "main", "deploy", "android",
                                   env=self.env(FAKE_ADB_DEVICES="a,device;b,device"))
        self.check(json.loads(result.stdout), "ambiguous device")


if __name__ == "__main__":
    unittest.main()
