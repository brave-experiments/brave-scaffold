# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Documentation checks: links resolve, examples parse, and documented commands match the parser."""

import json
import re
import shlex
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests.schema_validation import Validator
from tests.support import SCRIPTS

ROOT = SCRIPTS.parent
DOCUMENTS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]
FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def slug(heading):
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def headings(path):
    return {slug(match.group(1)) for match in re.finditer(r"^#+\s+(.*)$", path.read_text(encoding="utf-8"),
                                                          re.MULTILINE)}


class DocumentationTests(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        problems = []
        for document in DOCUMENTS:
            for target in LINK.findall(document.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, anchor = target.partition("#")
                path = (document.parent / file_part).resolve() if file_part else document
                if not path.exists():
                    problems.append("%s -> %s (missing)" % (document.name, target))
                elif anchor and path.suffix == ".md" and anchor not in headings(path):
                    problems.append("%s -> %s (no such heading)" % (document.name, target))
        self.assertEqual(problems, [])

    def test_no_document_is_empty_and_the_required_guides_exist(self):
        for name in ("getting-started", "configuration-and-environments", "commands", "troubleshooting",
                     "agent-workflows", "development"):
            path = ROOT / "docs" / (name + ".md")
            self.assertGreater(len(path.read_text().split()), 80, path)

    def test_toml_and_json_examples_are_valid(self):
        from scaffold.common import config as config_module
        checked = 0
        for document in DOCUMENTS:
            for language, body in FENCE.findall(document.read_text(encoding="utf-8")):
                if language == "toml":
                    tomllib.loads(body)
                    if body.lstrip().startswith("schema_version"):
                        with tempfile.TemporaryDirectory() as directory:
                            path = Path(directory) / "brave-scaffold.toml"
                            path.write_text(body)
                            config_module.load_config(path)
                    checked += 1
                elif language == "json":
                    value = json.loads(body)
                    if isinstance(value, dict) and "schema_version" in value:
                        self.assertEqual(Validator().problems(value), [], document.name)
                    checked += 1
        self.assertGreaterEqual(checked, 2)
        example = (ROOT / "brave-scaffold.example.toml").read_text()
        tomllib.loads(example)

    def test_documented_command_lines_match_the_parser(self):
        from scaffold.brave import bcore, bpm
        from scaffold.common.cli import parse_leading
        commands = 0
        for document in DOCUMENTS:
            for language, body in FENCE.findall(document.read_text(encoding="utf-8")):
                if language not in ("sh", "bash", "shell", ""):
                    continue
                for line in body.splitlines():
                    line = re.sub(r"\s+#.*$", "", line.strip())
                    match = re.match(r"^(?:scripts/)?(bcore|bpm)\s+(.*)$", line)
                    if not match or any(mark in line for mark in ("<", "...", "|")):
                        continue
                    words = shlex.split(match.group(2))
                    with self.subTest(document=document.name, line=line):
                        if match.group(1) == "bpm":
                            parse_leading(bpm.SPEC, words)
                        else:
                            spec, tokens = bcore.resolve_command(words)
                            bcore.parse_command(spec, tokens)
                        commands += 1
        self.assertGreater(commands, 15)

    def test_every_registered_command_is_documented(self):
        from scaffold.brave.registry import REGISTRY
        text = (ROOT / "docs" / "commands.md").read_text()
        for spec in {id(s): s for s in REGISTRY.values()}.values():
            self.assertTrue(spec.name in text, spec.name)

    def test_documents_do_not_carry_local_paths_or_planning_references(self):
        forbidden = re.compile(r"/Users/|/home/[a-z]|handoff|predecessor", re.IGNORECASE)
        for document in DOCUMENTS:
            if document.name != "AGENTS.md":
                self.assertIsNone(forbidden.search(document.read_text(encoding="utf-8")), document.name)


if __name__ == "__main__":
    unittest.main()
