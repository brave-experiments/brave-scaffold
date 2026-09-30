# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Strict checkout-local package execution and direct Python."""

import json
import os
import shutil
import unittest
from pathlib import Path

from tests.support import SCRIPTS, SandboxTest, write_executable

GLOBAL_TOOL = "#!/bin/sh\necho global-%s-used >> '%s'\nexit 0\n"


@unittest.skipUnless(shutil.which("direnv"), "direnv is required")
class PackageExecutionTests(SandboxTest):
    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        self.config = str(self.sandbox.config)
        for name in ("node", "npm", "pnpm", "vpython3"):
            write_executable(self.sandbox.bin / name, GLOBAL_TOOL % (name, self.sandbox.root / "global-used"))

    def bpm(self, *args, **kwargs):
        return self.sandbox.bdev("--config", self.config, "--checkout", "main", *args, tool="bpm", **kwargs)

    def test_runs_local_node_and_manager_in_core_despite_global_tools(self):
        result = self.bpm("run", "build", cwd=self.core.parent)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.sandbox.records()[0]
        self.assertEqual(record["cwd"], str(self.core))
        manager_entry = self.core / "third_party" / "node" / "node_modules" / "pnpm" / "bin" / "pnpm.mjs"
        self.assertEqual(record["argv"], [str(manager_entry), "run", "build"])
        node_bin = self.core / "third_party" / "node" / "node-mac-arm64" / "bin"
        self.assertEqual(record["path"].split(os.pathsep)[1], str(node_bin))
        self.assertEqual(record["launcher"], str(self.core))
        self.assertFalse((self.sandbox.root / "global-used").exists())

    def test_the_pnpm_shim_works_from_a_checkout_whose_path_has_an_apostrophe_and_a_space(self):
        core = self.sandbox.make_checkout("owner's checkout")
        self.sandbox.write_config([("odd", core, "environments/odd")])
        self.sandbox.bdev("env", "init", "--checkout", "odd", "--config", self.config)
        self.sandbox.approve("odd")
        hook = self.sandbox.hook("import subprocess\nif 'outer' in argv:\n    subprocess.run(['pnpm', 'inner', '--flag'], check=True)\n")
        result = self.sandbox.bdev("--config", self.config, "--checkout", "odd", "run", "outer", tool="bpm",
                                   env=self.sandbox.env(FAKE_HOOK=hook))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["argv"][1:] for r in self.sandbox.records()], [["run", "outer"], ["inner", "--flag"]])

    def test_arguments_after_the_first_package_argument_are_forwarded_exactly(self):
        arguments = ["run", "test", "--filter", "", "--name=with space", "-5", "--json", "--checkout", "other",
                     "café ☕", "$(touch pwned)", "a;b|c&d", "--", "--json", "--flag", "--flag", "--flag=x"]
        result = self.bpm(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.sandbox.records()[0]["argv"][1:]
        self.assertEqual(argv, arguments)
        self.assertFalse((self.sandbox.root / "pwned").exists())
        self.assertNotIn('"schema_version"', result.stdout, "child --json must not select scaffold JSON")

    def test_leading_delimiter_forwards_help_and_json_to_the_package_manager(self):
        result = self.bpm("--", "--help")
        self.assertEqual(self.sandbox.records()[0]["argv"][1:], ["--help"])
        self.assertIn("Usage", self.sandbox.bdev("--help", tool="bpm").stdout)

    def test_json_mode_keeps_child_output_off_stdout(self):
        env = self.sandbox.env(FAKE_STDOUT="child-output-line")
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x",
                                   tool="bpm", env=env)
        document = json.loads(result.stdout)
        self.assertEqual(document["status"], "ok")
        self.assertIn("child-output-line", result.stderr)
        self.assertEqual(document["data"]["cwd"], str(self.core))
        self.assertEqual(document["data"]["requested_arguments"], ["run", "x"])
        self.assertEqual(document["child_exit_code"], 0)

    def test_failing_child_reports_its_exit_separately(self):
        env = self.sandbox.env(FAKE_EXIT="3")
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x",
                                   tool="bpm", env=env)
        document = json.loads(result.stdout)
        self.assertEqual((result.returncode, document["exit_code"], document["child_exit_code"]), (5, 5, 3))
        self.assertEqual(document["error"]["code"], "CHILD_FAILED")

    def test_command_log_is_on_by_default_with_absolute_cwd_and_quoting(self):
        result = self.bpm("run", "test", "--name=two words", "--token=hunter2", "--password", "hunter3")
        self.assertIn("Current directory: %s" % self.core, result.stderr)
        self.assertIn("'--name=two words'", result.stderr)
        self.assertIn("--token=***", result.stderr)
        self.assertNotIn("hunter2", result.stderr)
        self.assertNotIn("hunter3", result.stderr)
        received = self.sandbox.records()[0]["argv"]
        self.assertIn("--token=hunter2", received, "redaction applies to logs only")

    def test_command_log_opt_out_keeps_results(self):
        self.sandbox.config.write_text(self.sandbox.config.read_text() + "\n[logging]\ncommands = false\n")
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x",
                                   tool="bpm")
        self.assertNotIn("Current directory", result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["status"], "ok")

    def test_older_npm_declaration_gets_a_separator_and_pnpm_does_not(self):
        older_npm = self.sandbox.make_checkout("older_npm", declaration=False)
        self.sandbox.write_config([("main", self.core, "environments/main"),
                                   ("older_npm", older_npm, "environments/older_npm")])
        self.sandbox.bdev("env", "init", "--checkout", "older_npm", "--config", self.config)
        self.sandbox.approve("older_npm")
        self.sandbox.bdev("--config", self.config, "--checkout", "older_npm", "run", "sync", "--force", "", "a b",
                          tool="bpm")
        self.sandbox.bdev("--config", self.config, "--checkout", "older_npm", "run", "sync", "--", "--force",
                          tool="bpm")
        self.sandbox.bdev("--config", self.config, "--checkout", "older_npm", "install", tool="bpm")
        self.bpm("run", "sync", "--force")
        argvs = [record["argv"][1:] for record in self.sandbox.records()]
        self.assertEqual(argvs[0], ["run", "sync", "--", "--force", "", "a b"])
        self.assertEqual(argvs[1], ["run", "sync", "--", "--force"])
        self.assertEqual(argvs[2], ["install"])
        self.assertEqual(argvs[3], ["run", "sync", "--force"])
        self.assertTrue(self.sandbox.records()[0]["argv"][0].endswith("npm-cli.js"))

    def test_missing_local_node_never_falls_back_to_a_global_one(self):
        node = self.core / "third_party" / "node" / "node-mac-arm64" / "bin" / "node"
        node.unlink()
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x", tool="bpm")
        document = json.loads(result.stdout)
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "LOCAL_TOOL_MISSING"))
        self.assertEqual(document["error"]["repairs"][0]["argv"][:3], ["bdev", "tools", "setup"])
        self.assertEqual(self.sandbox.records(), [])
        self.assertFalse((self.sandbox.root / "global-used").exists())

    def blocked(self, name="main"):
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", name, "run", "x", tool="bpm")
        document = json.loads(result.stdout)
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "LOCAL_TOOL_MISSING"), result.stderr)
        self.assertEqual(self.sandbox.records(), [], "the package command never started")
        self.assertFalse((self.sandbox.root / "global-used").exists())
        return document

    def test_tools_that_resolve_outside_the_checkout_payload_are_rejected(self):
        node_dir = self.core / "third_party" / "node" / "node-mac-arm64"
        external = self.sandbox.root / "external"
        for relative in ("bin/node", "bin/version"):
            (external / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(node_dir / relative, external / relative)
        node = node_dir / "bin" / "node"
        node.unlink()
        node.symlink_to(external / "bin" / "node")
        self.assertIn("outside", json.dumps(self.blocked()["error"]))
        node.unlink()
        shutil.copy(external / "bin" / "node", node)
        pnpm_entry = self.core / "third_party" / "node" / "node_modules" / "pnpm" / "bin" / "pnpm.mjs"
        pnpm_entry.unlink()
        (external / "pnpm.mjs").write_text("")
        pnpm_entry.symlink_to(external / "pnpm.mjs")
        self.assertIn("outside", json.dumps(self.blocked()["error"]))

    def test_a_vpython3_that_resolves_outside_the_checkout_is_not_used(self):
        vpython = self.core / "vendor" / "depot_tools" / "vpython3"
        external = self.sandbox.root / "external-vpython3"
        external.write_text(vpython.read_text())
        external.chmod(0o755)
        vpython.unlink()
        vpython.symlink_to(external)
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x", tool="bpm")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.sandbox.records(), [])

    def test_links_that_stay_inside_the_payload_are_fine(self):
        node_dir = self.core / "third_party" / "node" / "node-mac-arm64"
        real = self.core / "third_party" / "node" / "node-v0-real"
        node_dir.rename(real)
        node_dir.symlink_to(real)
        result = self.bpm("run", "x")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_payloads_that_cannot_be_verified_stop_execution(self):
        unverifiable = self.sandbox.make_checkout("nometa", payload_metadata=False)
        unreadable = self.sandbox.make_checkout("broken")
        (unreadable / "tools" / "cr" / "extra_deps.py").write_text("this is not python (\n")
        self.sandbox.write_config([("main", self.core, "environments/main"), ("nometa", unverifiable, "environments/nometa"),
                                   ("broken", unreadable, "environments/broken")])
        for name in ("nometa", "broken"):
            self.sandbox.bdev("env", "init", "--checkout", name, "--config", self.config)
            self.sandbox.approve(name)
            with self.subTest(name):
                document = self.blocked(name)
                self.assertIn("verif", document["error"]["message"])
        result = self.sandbox.bdev("--json", "--config", self.config, "doctor", "mac", "--checkout", "nometa")
        local = next(check for check in json.loads(result.stdout)["checks"] if check["name"] == "local-tools")
        self.assertEqual(local["status"], "blocker")
        nested = {check["name"]: check["status"] for check in local["evidence"]["checks"]}
        self.assertEqual(nested["payload-freshness"], "blocker")

    def test_older_npm_is_verified_by_its_own_payload_not_pnpms(self):
        older_npm = self.sandbox.make_checkout("older_npm", declaration=False)
        self.sandbox.write_config([("main", self.core, "environments/main"),
                                   ("older_npm", older_npm, "environments/older_npm")])
        self.sandbox.bdev("env", "init", "--checkout", "older_npm", "--config", self.config)
        self.sandbox.approve("older_npm")
        self.sandbox.mark_stale("older_npm", only="pnpm")
        result = self.sandbox.bdev("--config", self.config, "--checkout", "older_npm", "run", "x", tool="bpm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.sandbox.mark_stale("older_npm", only="node")
        self.sandbox.record.unlink()
        self.assertEqual(self.blocked("older_npm")["error"]["code"], "LOCAL_TOOL_MISSING")

    def test_stale_payload_stops_before_the_command_and_names_the_repair(self):
        self.sandbox.mark_stale("main")
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x", tool="bpm")
        document = json.loads(result.stdout)
        self.assertEqual(document["error"]["code"], "LOCAL_TOOL_MISSING")
        self.assertEqual(self.sandbox.records(), [])

    def test_inspection_never_runs_the_installer(self):
        self.sandbox.mark_stale("main")
        self.sandbox.bdev("context", "--checkout", "main", "--config", self.config)
        self.sandbox.bdev("doctor", "mac", "--checkout", "main", "--config", self.config)
        self.assertEqual([r for r in self.sandbox.records() if r["tool"] == "installer"], [])

    def test_explicit_tools_setup_runs_the_installer_then_revalidates(self):
        self.sandbox.mark_stale("main")
        result = self.sandbox.bdev("--json", "tools", "setup", "--checkout", "main", "--config", self.config)
        document = json.loads(result.stdout)
        self.assertEqual(document["status"], "ok", document)
        installs = [r for r in self.sandbox.records() if r["tool"] == "installer"]
        self.assertEqual(len(installs), 2)
        ok = self.bpm("run", "x")
        self.assertEqual(ok.returncode, 0)

    def test_node_outside_the_declared_range_is_not_usable(self):
        (self.core / "third_party" / "node" / "node-mac-arm64" / "bin" / "version").write_text("v22.1.0")
        result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x", tool="bpm")
        document = json.loads(result.stdout)
        self.assertEqual(document["error"]["code"], "LOCAL_TOOL_MISSING")
        self.assertIn("v22.1.0", document["error"]["message"])

    def test_malformed_and_unsupported_declarations_are_errors(self):
        package = self.core / "package.json"
        for text in ('{"name": "brave-core", "devEngines": {"packageManager": {"name": "yarn"}}}',
                     '{"name": "brave-core", "devEngines": {"packageManager": "pnpm"}}',
                     '{"name": "brave-core", "devEngines": []}'):
            with self.subTest(text=text):
                package.write_text(text)
                result = self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x",
                                           tool="bpm")
                document = json.loads(result.stdout)
                self.assertEqual(document["error"]["code"], "LOCAL_TOOL_MISSING")
                self.assertIn("package.json", json.dumps(document["error"]["details"]))
        self.assertEqual(self.sandbox.records(), [])

    def test_termination_stops_the_child_and_reports_cancellation(self):
        import signal
        import subprocess
        import time
        env = self.sandbox.env(FAKE_SLEEP="60")
        process = subprocess.Popen(
            [str(SCRIPTS / "bpm"), "--json", "--config", self.config, "--checkout", "main", "run", "slow"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not self.sandbox.records() and time.time() < deadline:
            time.sleep(0.05)
        child = self.sandbox.records()[0]["pid"]
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=30)
        document = json.loads(stdout)
        self.assertEqual((process.returncode, document["status"], document["exit_code"]), (143, "cancelled", 143))
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)


REJECTS_PNPM = """#!%(python)s
import json, os, sys
with open(os.environ["FAKE_RECORD"], "a") as stream:
    stream.write(json.dumps({"tool": "installer", "argv": sys.argv[1:]}) + "\\n")
if sys.argv[1].endswith("node_modules"):
    sys.exit(7)
import glob
workspace = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
for stale in glob.glob(os.path.join(workspace, "STALE*")):
    os.unlink(stale)
"""

RESTORES_PNPM = """#!%(python)s
import glob, json, os, sys
with open(os.environ["FAKE_RECORD"], "a") as stream:
    stream.write(json.dumps({"tool": "installer", "argv": sys.argv[1:]}) + "\\n")
core = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.argv[1].endswith("node_modules"):
    pnpm = os.path.join(core, "third_party", "node", "node_modules", "pnpm")
    os.makedirs(os.path.join(pnpm, "bin"), exist_ok=True)
    open(os.path.join(pnpm, "bin", "pnpm.mjs"), "w").write("")
    open(os.path.join(pnpm, "package.json"), "w").write('{"version": "11.25.0"}')
workspace = os.path.join(core, "..", "..")
for stale in glob.glob(os.path.join(workspace, "STALE*")):
    os.unlink(stale)
"""


@unittest.skipUnless(shutil.which("direnv"), "direnv is required")
class ToolRepairTests(SandboxTest):
    """Repair installs exactly what inspection requires for the checkout's package manager."""

    def setUp(self):
        super().setUp()
        self.config = str(self.sandbox.config)

    def checkout(self, name, **options):
        core = self.sandbox.make_checkout(name, **options)
        entries = [(other, path, "environments/" + other) for other, path in self.sandbox.checkouts.items()]
        self.sandbox.write_config(entries)
        self.sandbox.bdev("env", "init", "--checkout", name, "--config", self.config)
        self.sandbox.approve(name)
        return core

    def setup_tools(self, name):
        result = self.sandbox.bdev("--json", "tools", "setup", "--checkout", name, "--config", self.config)
        return result, json.loads(result.stdout)

    def installed(self):
        return [record["argv"][0].rsplit("/", 1)[-1] for record in self.sandbox.records() if record["tool"] == "installer"]

    def test_older_npm_repair_installs_only_the_node_payload_that_contains_npm(self):
        core = self.checkout("older", declaration=False)
        write_executable(core / "tools" / "cr" / "tarball_installer.py", REJECTS_PNPM)
        self.sandbox.mark_stale("older", only="node")
        result, document = self.setup_tools("older")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stdout)
        self.assertEqual(self.installed(), ["node-mac-arm64"])
        self.assertTrue(document["data"]["ready"])

    def test_npm_with_a_current_node_payload_needs_no_pnpm_entry(self):
        core = self.checkout("older", declaration=False, manager="npm")
        write_executable(core / "tools" / "cr" / "tarball_installer.py", REJECTS_PNPM)
        result, document = self.setup_tools("older")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.installed(), ["node-mac-arm64"])

    def test_pnpm_repair_installs_node_and_the_missing_manager_payload_then_checks_both(self):
        core = self.checkout("modern")
        write_executable(core / "tools" / "cr" / "tarball_installer.py", RESTORES_PNPM)
        shutil.rmtree(core / "third_party" / "node" / "node_modules")
        self.sandbox.mark_stale("modern", only="pnpm")
        result, document = self.setup_tools("modern")
        self.assertEqual((result.returncode, document["status"]), (0, "ok"), result.stdout)
        self.assertEqual(self.installed(), ["node-mac-arm64", "node_modules"])

    def test_a_failing_required_entry_is_reported_with_its_own_exit(self):
        core = self.checkout("modern")
        write_executable(core / "tools" / "cr" / "tarball_installer.py", REJECTS_PNPM)
        self.sandbox.mark_stale("modern")
        result, document = self.setup_tools("modern")
        self.assertEqual((result.returncode, document["error"]["code"], document["child_exit_code"]),
                         (5, "CHILD_FAILED", 7))

    def test_a_missing_local_python_points_at_the_sync_that_installs_depot_tools(self):
        core = self.checkout("modern")
        shutil.rmtree(core / "vendor" / "depot_tools")
        result = self.sandbox.bdev("--json", "vpython3", "--checkout", "modern", "--config", self.config, "--", "a.py")
        argv = [step["argv"][:3] for step in json.loads(result.stdout)["error"]["repairs"]]
        self.assertNotIn(["bdev", "tools", "setup"], argv, "tools setup installs Node and the package manager only")
        self.assertIn(["bdev", "sync", "--checkout"], argv)


@unittest.skipUnless(shutil.which("direnv"), "direnv is required")
class ToolLocalityTests(SandboxTest):
    """Tools must live inside the selected checkout, however the directories that hold them are linked."""

    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        self.config = str(self.sandbox.config)
        self.external = self.sandbox.root / "external"
        self.external.mkdir()

    def move_out_and_link_back(self, path, name):
        target = self.external / name
        shutil.move(str(path), str(target))
        path.symlink_to(target)

    def bpm(self):
        return self.sandbox.bdev("--json", "--config", self.config, "--checkout", "main", "run", "x", tool="bpm")

    def assert_refused(self):
        result = self.bpm()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.sandbox.records(), [], "no foreign tool ran")
        return json.loads(result.stdout)

    def test_a_whole_node_payload_directory_linked_from_outside_is_rejected(self):
        self.move_out_and_link_back(self.core / "third_party" / "node", "node")
        self.assertIn("outside", json.dumps(self.assert_refused()["error"]))

    def test_a_whole_depot_tools_directory_linked_from_outside_is_rejected(self):
        self.move_out_and_link_back(self.core / "vendor" / "depot_tools", "depot_tools")
        self.assert_refused()
        result = self.sandbox.bdev("--json", "vpython3", "--config", self.config, "--checkout", "main", "--", "a.py")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.sandbox.records(), [])

    def test_a_linked_ancestor_of_the_payload_is_rejected(self):
        self.move_out_and_link_back(self.core / "third_party", "third_party")
        self.assert_refused()

    def test_another_checkouts_payload_is_rejected(self):
        other = self.sandbox.make_checkout("other")
        node_dir = self.core / "third_party" / "node" / "node-mac-arm64"
        shutil.rmtree(node_dir)
        node_dir.symlink_to(other / "third_party" / "node" / "node-mac-arm64")
        self.assertIn("outside", json.dumps(self.assert_refused()["error"]))

    def test_links_that_stay_inside_the_checkout_are_fine(self):
        node = self.core / "third_party" / "node"
        node.rename(self.core / "third_party" / "node-real")
        node.symlink_to(self.core / "third_party" / "node-real")
        depot = self.core / "vendor" / "depot_tools"
        depot.rename(self.core / "vendor" / "depot_real")
        depot.symlink_to(self.core / "vendor" / "depot_real")
        result = self.sandbox.bdev("--config", self.config, "--checkout", "main", "run", "x", tool="bpm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.sandbox.records()), 1)

    def test_an_environment_cannot_supply_a_python_the_checkout_lacks(self):
        foreign = self.external / "depot_tools"
        shutil.move(str(self.core / "vendor" / "depot_tools"), str(foreign))
        directory = self.sandbox.root / "config" / "environments" / "main"
        (directory / ".envrc").write_text("\n".join([
            'export BRAVE_BROWSER_DIR="%s"' % self.core.parents[3], 'export BRAVE_SRC_ROOT="%s"' % self.core.parent,
            'export BRAVE_CORE_DIR="%s"' % self.core, 'export BRAVE_DEPOT_TOOLS_DIR="%s"' % foreign,
            'export VPYTHON3="%s/vpython3"' % foreign, ""]))
        self.sandbox.approve("main")
        result = self.sandbox.bdev("--json", "vpython3", "--config", self.config, "--checkout", "main", "--", "a.py")
        document = json.loads(result.stdout)
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "LOCAL_TOOL_MISSING"), result.stderr)
        self.assertEqual(self.sandbox.records(), [], "the foreign interpreter never ran")
        self.assertEqual(self.bpm().returncode, 3)
        self.assertEqual(self.sandbox.records(), [])

    def test_repair_does_not_write_through_a_payload_directory_that_leaves_the_checkout(self):
        self.move_out_and_link_back(self.core / "third_party" / "node", "node")
        self.sandbox.mark_stale("main")
        result = self.sandbox.bdev("--json", "tools", "setup", "--checkout", "main", "--config", self.config)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([r for r in self.sandbox.records() if r["tool"] == "installer"], [])


@unittest.skipUnless(shutil.which("direnv"), "direnv is required")
class DirectPythonTests(SandboxTest):
    def setUp(self):
        super().setUp()
        self.core = self.sandbox.make_checkout("main")
        self.sandbox.prepare_environment("main")
        self.config = str(self.sandbox.config)

    def vpython(self, *args, cwd):
        return self.sandbox.bdev("vpython3", "--config", self.config, "--checkout", "main", *args, cwd=cwd)

    def test_caller_cwd_is_preserved_from_unrelated_and_nested_directories(self):
        unrelated = self.sandbox.root / "elsewhere"
        unrelated.mkdir()
        nested = self.core.parent / "chrome" / "test"
        nested.mkdir(parents=True)
        for directory in (unrelated, nested, self.core.parent):
            with self.subTest(directory=directory):
                result = self.vpython("--", "tools/example.py", "--flag", "", "a b", cwd=directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self.sandbox.records()[-1]
                self.assertEqual(record["cwd"], str(directory))
                self.assertEqual(record["argv"], ["tools/example.py", "--flag", "", "a b"])

    def test_explicit_cwd_is_relative_to_the_callers_directory(self):
        base = self.sandbox.root / "base"
        (base / "sub").mkdir(parents=True)
        absolute = self.sandbox.root / "absolute"
        absolute.mkdir()
        self.vpython("--cwd", "sub", "--", "x.py", cwd=base)
        self.vpython("--cwd", str(absolute), "--", "y.py", cwd=base)
        first, second = self.sandbox.records()
        self.assertEqual(first["cwd"], str(base / "sub"))
        self.assertEqual(second["cwd"], str(absolute))
        missing = self.sandbox.bdev("--json", "vpython3", "--config", self.config, "--checkout", "main",
                                    "--cwd", "nope", "--", "z.py", cwd=base)
        self.assertEqual(json.loads(missing.stdout)["error"]["code"], "INVALID_INPUT")
        self.assertEqual(len(self.sandbox.records()), 2)

    def test_arguments_that_look_like_scaffold_options_reach_python(self):
        self.vpython("script.py", "--json", "--checkout", "other", cwd=self.sandbox.root)
        record = self.sandbox.records()[-1]
        self.assertEqual(record["argv"], ["script.py", "--json", "--checkout", "other"])

    def test_result_reports_interpreter_and_execution_cwd(self):
        result = self.sandbox.bdev("--json", "vpython3", "--config", self.config, "--checkout", "main", "--",
                                   "a.py", cwd=self.sandbox.root)
        data = json.loads(result.stdout)["data"]
        self.assertEqual(data["interpreter"], str(self.core / "vendor" / "depot_tools" / "vpython3"))
        self.assertEqual(data["cwd"], str(self.sandbox.root))


if __name__ == "__main__":
    unittest.main()
