# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Configuration parsing, validation, and in-place record edits."""

import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.common import config as config_module
from scaffold.common.results import ScaffoldError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.path = self.dir / "brave-scaffold.toml"

    def load(self, text):
        self.path.write_text(text)
        return config_module.load_config(self.path)

    def fails(self, text, field):
        with self.assertRaises(ScaffoldError) as caught:
            self.load(text)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertEqual(caught.exception.details["field"], field)
        return caught.exception

    def test_valid_configuration_resolves_environment_beside_the_file(self):
        config = self.load('schema_version = 1\n[defaults]\nplatform = "MacOS"\n'
                           '[[checkouts]]\nalias = "main"\ncore = "/work/a/src/brave"\ndirenv_dir = "environments/main"\n')
        self.assertEqual(config.default_platform, "macos")
        self.assertEqual(config.checkouts[0].direnv_dir, self.dir / "environments" / "main")
        self.assertTrue(config.commands_logging)

    def test_missing_default_file_is_empty_but_an_explicit_missing_file_is_an_error(self):
        self.assertFalse(config_module.load_config(self.path).exists)
        with self.assertRaises(ScaffoldError):
            config_module.load_config(self.path, explicit=True)

    def test_unknown_fields_wrong_types_and_versions_name_the_field_and_an_example(self):
        self.fails("schema_version = 2\n", "schema_version")
        self.fails("schema_version = 1\nbogus = 1\n", "bogus")
        self.fails("schema_version = 1\n[logging]\ncommands = 'yes'\n", "logging.commands")
        self.fails('schema_version = 1\n[defaults]\nplatform = "linux"\n', "defaults.platform")
        error = self.fails('schema_version = 1\n[[checkouts]]\ncore = "relative/path"\n', "checkouts[0].core")
        self.assertIn("example", error.details)
        self.fails('schema_version = 1\n[[checkouts]]\ncore = "/a/src/brave"\nextra = 1\n', "checkouts[0].extra")

    def test_default_android_device_is_validated(self):
        config = self.load('schema_version = 1\n[defaults]\nandroid_device = "emulator-5554"\n')
        self.assertEqual(config.default_android_device, "emulator-5554")
        self.fails("schema_version = 1\n[defaults]\nandroid_device = 5\n", "defaults.android_device")

    def test_duplicates_are_rejected(self):
        base = 'schema_version = 1\n[[checkouts]]\nalias = "a"\ncore = "/x/src/brave"\ndirenv_dir = "e/a"\n'
        self.fails(base + '[[checkouts]]\nalias = "a"\ncore = "/y/src/brave"\n', "checkouts[1].alias")
        self.fails(base + '[[checkouts]]\ncore = "/x/src/brave"\n', "checkouts[1].core")
        self.fails(base + '[[checkouts]]\ncore = "/y/src/brave"\ndirenv_dir = "e/a"\n', "checkouts[1].direnv_dir")

    def test_upsert_adds_then_extends_one_record_without_touching_other_text(self):
        original = '# my notes\nschema_version = 1\n\n[logging]\ncommands = false\n\n' \
                   '[[checkouts]]\ncore = "/x/src/brave"\n\n[[checkouts]]\nalias = "b"\ncore = "/y/src/brave"\n'
        self.path.write_text(original)
        self.assertEqual(config_module.upsert_checkout(self.path, "/x/src/brave", alias="a"), "updated")
        self.assertEqual(config_module.upsert_checkout(self.path, "/x/src/brave", direnv_dir="environments/a"),
                         "updated")
        self.assertEqual(config_module.upsert_checkout(self.path, "/x/src/brave", alias="a"), "unchanged")
        text = self.path.read_text()
        self.assertTrue(text.startswith("# my notes\n"))
        config = config_module.load_config(self.path)
        self.assertEqual([(c.alias, c.direnv_dir_text) for c in config.checkouts],
                         [("a", "environments/a"), ("b", None)])
        self.assertFalse(config.commands_logging)
        self.assertIn('alias = "b"\ncore = "/y/src/brave"\n', text)

    def test_upsert_creates_the_file_and_refuses_to_replace_a_different_alias(self):
        self.assertEqual(config_module.upsert_checkout(self.path, "/x/src/brave", alias="a"), "added")
        self.assertEqual(config_module.load_config(self.path).checkouts[0].alias, "a")
        with self.assertRaises(ScaffoldError):
            config_module.upsert_checkout(self.path, "/x/src/brave", alias="other")


if __name__ == "__main__":
    unittest.main()
