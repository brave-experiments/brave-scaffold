# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Result envelope, help, setup, and checkout registration through the real CLI."""

import json
import shlex
import unittest

from tests.support import SandboxTest, tree_snapshot

ENVELOPE_KEYS = ["schema_version", "status", "command", "operation_id", "context", "data", "checks", "warnings",
                 "error", "artifacts", "logs", "exit_code", "child_exit_code"]


class EnvelopeTests(SandboxTest):
    def test_success_and_failure_share_the_envelope_and_exit_codes(self):
        ok, document = self.sandbox.bdev_json("capabilities")
        self.assertEqual(list(document), ENVELOPE_KEYS)
        self.assertEqual((ok.returncode, document["exit_code"], document["error"]), (0, 0, None))
        bad, failure = self.sandbox.bdev_json("env", "check")
        self.assertEqual(list(failure), ENVELOPE_KEYS)
        self.assertEqual((bad.returncode, failure["exit_code"], failure["status"]), (2, 2, "error"))

    def test_parse_errors_still_produce_exactly_one_json_document(self):
        for args in (["nonsense"], ["context", "--bogus"], ["context", "--checkout"], ["env"], ["doctor", "a", "b"],
                     ["context", "--format", "yaml"]):
            with self.subTest(args=args):
                result = self.sandbox.bdev("--json", *args)
                document = json.loads(result.stdout)
                self.assertEqual((result.returncode, document["error"]["code"]), (2, "INVALID_INPUT"))

    def test_json_flag_position_is_free_up_to_the_delimiter(self):
        for args in (["--json", "capabilities"], ["capabilities", "--json"], ["capabilities", "--format", "json"],
                     ["--format=json", "capabilities"]):
            with self.subTest(args=args):
                self.assertEqual(json.loads(self.sandbox.bdev(*args).stdout)["command"], "capabilities")

    def test_unknown_command_suggests_the_closest_names(self):
        result, document = self.sandbox.bdev_json("contxt")
        self.assertIn("context", document["error"]["details"]["did_you_mean"])

    def test_help_goes_to_stdout_and_lists_side_effects(self):
        result = self.sandbox.bdev("env", "init", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Side effects:", result.stdout)
        self.assertIn("Never approves", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_every_help_example_parses(self):
        from scaffold.brave import bdev, bpm
        from scaffold.brave.registry import REGISTRY
        from scaffold.common.cli import parse_leading
        for spec in REGISTRY.values():
            for example in spec.examples:
                with self.subTest(example=example):
                    words = shlex.split(example)
                    self.assertEqual(words[0], "bdev")
                    found, tokens = bdev.resolve_command(words[1:])
                    bdev.parse_command(found, tokens)
        for example in bpm.SPEC.examples:
            words = shlex.split(example)
            parse_leading(bpm.SPEC, words[1:])


def conforms(schema, value, root, path="$"):
    """Small subset of JSON Schema: $ref, const, enum, type, required, properties, items, oneOf."""
    problems = []
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return conforms(node, value, root, path)
    if "oneOf" in schema:
        if not any(not conforms(option, value, root, path) for option in schema["oneOf"]):
            problems.append("%s matches no allowed shape" % path)
        return problems
    if "const" in schema and value != schema["const"]:
        problems.append("%s must be %r" % (path, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        problems.append("%s=%r not in %r" % (path, value, schema["enum"]))
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        kinds = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
        if not any(isinstance(value, kinds[name]) for name in names):
            problems.append("%s has the wrong type" % path)
    if isinstance(value, dict):
        problems += ["%s.%s is missing" % (path, key) for key in schema.get("required", []) if key not in value]
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                problems += conforms(sub, value[key], root, "%s.%s" % (path, key))
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            problems += conforms(schema["items"], item, root, "%s[%d]" % (path, index))
    return problems


class SchemaTests(SandboxTest):
    def test_results_conform_to_the_published_envelope_schema(self):
        from tests.support import SCRIPTS
        schema = json.loads((SCRIPTS / "schemas" / "result-envelope.schema.json").read_text())
        core = self.sandbox.make_checkout("main")
        self.sandbox.write_config([("main", core, "environments/main")])
        config = str(self.sandbox.config)
        outputs = [self.sandbox.bdev_json("capabilities")[1],
                   self.sandbox.bdev_json("env", "check", "--config", config)[1],
                   self.sandbox.bdev_json("doctor", "mac", "--checkout", "main", "--config", config)[1],
                   self.sandbox.bdev_json("context", "--checkout", "main", "--config", config)[1],
                   self.sandbox.bdev_json("nonsense")[1]]
        for document in outputs:
            with self.subTest(command=document["command"]):
                self.assertEqual(conforms(schema, document, schema), [])
        self.assertTrue(any(document["error"] and document["error"]["repairs"] for document in outputs))
        self.assertTrue(any(document["checks"] for document in outputs))


class SetupAndRegistrationTests(SandboxTest):
    def test_setup_creates_scaffold_configuration_only(self):
        core = self.sandbox.make_checkout("main")
        before = tree_snapshot(core.parents[3])
        target = self.sandbox.root / "fresh" / "brave-scaffold.toml"
        result, document = self.sandbox.bdev_json("setup", "--config", str(target))
        self.assertEqual(document["status"], "ok", document)
        self.assertTrue(target.is_file())
        self.assertEqual(before, tree_snapshot(core.parents[3]))
        self.assertTrue(any("direnv allow" in step for step in document["data"]["next_steps"]))

    def test_checkout_add_records_an_alias_and_rejects_conflicts(self):
        main = self.sandbox.make_checkout("main")
        other = self.sandbox.make_checkout("other")
        config = str(self.sandbox.config)
        first = self.sandbox.bdev("checkout", "add", "main", str(main.parents[3]), "--config", config)
        self.assertEqual(first.returncode, 0, first.stderr)
        again = self.sandbox.bdev("checkout", "add", "main", str(main), "--config", config)
        self.assertEqual(again.returncode, 0)
        clash, document = self.sandbox.bdev_json("checkout", "add", "main", str(other), "--config", config)
        self.assertEqual(document["error"]["code"], "CONFIG_INVALID")
        text = self.sandbox.config.read_text()
        self.assertEqual(text.count("[[checkouts]]"), 1)
        self.assertIn(str(main), text)

    def test_checkout_list_flags_invalid_registrations(self):
        main = self.sandbox.make_checkout("main")
        gone = self.sandbox.root / "gone" / "src" / "brave"
        self.sandbox.write_config([("main", main, "environments/main"), ("gone", gone, None)])
        result, document = self.sandbox.bdev_json("checkout", "list", "--config", str(self.sandbox.config))
        rows = {row["alias"]: row for row in document["data"]["checkouts"]}
        self.assertIsNone(rows["main"]["problem"])
        self.assertEqual(rows["main"]["environment_state"], "environment file missing")
        self.assertEqual(rows["gone"]["problem"], "path does not exist")

    def test_checkout_add_rejects_a_linked_worktree_layout(self):
        core = self.sandbox.make_checkout("wt")
        import shutil
        gitdir = self.sandbox.root / "common" / "worktrees" / "w"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        shutil.rmtree(core / ".git")
        (core / ".git").write_text("gitdir: %s\n" % gitdir)
        result, document = self.sandbox.bdev_json("checkout", "add", "wt", str(core),
                                                  "--config", str(self.sandbox.config))
        self.assertEqual(document["error"]["code"], "UNSUPPORTED_CAPABILITY")
        self.assertFalse(self.sandbox.config.exists())


if __name__ == "__main__":
    unittest.main()
