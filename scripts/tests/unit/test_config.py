# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Configuration parsing, validation, and in-place record edits."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(config.verbosity, "normal")

    def test_missing_default_file_is_empty_but_an_explicit_missing_file_is_an_error(self):
        self.assertFalse(config_module.load_config(self.path).exists)
        with self.assertRaises(ScaffoldError):
            config_module.load_config(self.path, explicit=True)

    def test_unknown_fields_wrong_types_and_versions_name_the_field_and_an_example(self):
        self.fails("schema_version = 2\n", "schema_version")
        self.fails("schema_version = 1\nbogus = 1\n", "bogus")
        self.fails("schema_version = 1\n[logging]\ncommands = true\n", "logging.commands")
        self.fails('schema_version = 1\n[defaults]\nplatform = "linux"\n', "defaults.platform")
        error = self.fails('schema_version = 1\n[[checkouts]]\ncore = ""\n', "checkouts[0].core")
        self.assertIn("example", error.details)
        self.fails('schema_version = 1\n[[checkouts]]\ncore = "/a/src/brave"\nextra = 1\n', "checkouts[0].extra")

    def test_relative_core_is_based_on_config_directory_and_upsert_preserves_it(self):
        config = self.load('schema_version = 1\n[[checkouts]]\ncore = "browser/src/brave"\n')
        expected = self.path.parent / "browser/src/brave"
        self.assertEqual(config.checkouts[0].core_real, expected.resolve())
        self.assertEqual(config_module.upsert_checkout(self.path, str(expected), alias="main"), "updated")
        self.assertIn('core = "browser/src/brave"', self.path.read_text())
        self.assertEqual(len(config_module.load_config(self.path).checkouts), 1)

    def test_core_expands_the_home_directory_like_the_other_paths(self):
        home = self.dir / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            config = self.load('schema_version = 1\n[[checkouts]]\ncore = "~/work/browser/src/brave"\n')
        expected = home / "work" / "browser" / "src" / "brave"
        self.assertEqual(config.checkouts[0].core_real, expected.resolve())
        self.assertEqual(Path(config.checkouts[0].core), expected)

    def test_updating_a_checkout_recorded_with_a_tilde_path_finds_it_instead_of_adding_a_duplicate(self):
        home = self.dir / "home"
        (home / "proj" / "src" / "brave").mkdir(parents=True)
        self.path.write_text('schema_version = 1\n[[checkouts]]\ncore = "~/proj/src/brave"\n')
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            outcome = config_module.upsert_checkout(self.path, str(home / "proj" / "src" / "brave"), alias="main")
            loaded = config_module.load_config(self.path)
        self.assertEqual(outcome, "updated")
        self.assertEqual(self.path.read_text().count("[[checkouts]]"), 1)
        self.assertEqual([record.alias for record in loaded.checkouts], ["main"])
        self.assertIn('core = "~/proj/src/brave"', self.path.read_text(), "the user's own spelling is kept")

    def test_a_change_that_would_leave_the_configuration_invalid_is_never_written(self):
        first, second = self.dir / "a" / "src" / "brave", self.dir / "b" / "src" / "brave"
        self.path.write_text('schema_version = 1\n[[checkouts]]\nalias = "main"\ncore = "%s"\n' % first)
        before = self.path.read_text()
        with self.assertRaises(ScaffoldError) as caught:
            config_module.upsert_checkout(self.path, str(second), alias="main")
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertIn("nothing was written", caught.exception.message)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(len(config_module.load_config(self.path).checkouts), 1, "the file still loads")

    def update_alias(self, text):
        self.path.write_text(text)
        outcome = config_module.upsert_checkout(self.path, "/work/a/src/brave", alias="main", direnv_dir="env/main")
        loaded = config_module.load_config(self.path)
        self.assertEqual(outcome, "updated")
        self.assertEqual([(record.alias, str(record.core)) for record in loaded.checkouts],
                         [("main", "/work/a/src/brave")])
        self.assertEqual(self.path.read_text().count("[[checkouts]]"), 1)
        return self.path.read_text()

    def test_a_final_core_line_without_a_newline_is_extended_on_its_own_lines(self):
        text = self.update_alias('schema_version = 1\n\n[[checkouts]]\ncore = "/work/a/src/brave"')
        lines = text.splitlines()
        for assignment in ('core = "/work/a/src/brave"', 'alias = "main"', 'direnv_dir = "env/main"'):
            self.assertIn(assignment, lines, "each assignment is on a line of its own")

    def test_every_valid_spelling_of_the_core_key_can_be_updated(self):
        for core in ('"core" = "/work/a/src/brave"', "core='/work/a/src/brave'",
                     "core   =   \"/work/a/src/brave\"   # my main checkout", "'core' = '/work/a/src/brave'"):
            with self.subTest(core=core):
                text = self.update_alias("schema_version = 1\n[[checkouts]]\n%s\n" % core)
                self.assertIn('alias = "main"', text)
                self.assertIn(core, text, "the user's own line is kept as written")

    def test_a_block_followed_by_other_tables_and_comments_keeps_them(self):
        text = self.update_alias('schema_version = 1\n[[checkouts]]\n# my main one\ncore = "/work/a/src/brave"\n'
                                 '\n[logging]\nverbosity = "verbose"\n')
        self.assertIn("# my main one\n", text)
        self.assertTrue(text.endswith('[logging]\nverbosity = "verbose"\n'))

    def test_ios_is_a_valid_default_platform(self):
        config = self.load('schema_version = 1\n[defaults]\nplatform = "iOS"\n')
        self.assertEqual(config.default_platform, "ios")
        error = self.fails('schema_version = 1\n[defaults]\nplatform = "linux"\n', "defaults.platform")
        self.assertIn("ios", error.message)

    def test_android_support_location_is_optional_and_relative_to_config(self):
        self.assertIsNone(self.load('schema_version = 1\n').android_support_path)
        config = self.load('schema_version = 1\nandroid_support_path = "dependencies/android"\n')
        self.assertEqual(config.android_support_path, self.path.parent / "dependencies/android")
        self.fails('schema_version = 1\nandroid_support_path = 5\n', "android_support_path")

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
        original = '# my notes\nschema_version = 1\n\n[logging]\nverbosity = "quiet"\n\n' \
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
        self.assertEqual(config.verbosity, "quiet")
        self.assertIn('alias = "b"\ncore = "/y/src/brave"\n', text)

    def test_upsert_creates_the_file_and_refuses_to_replace_a_different_alias(self):
        self.assertEqual(config_module.upsert_checkout(self.path, "/x/src/brave", alias="a"), "added")
        self.assertEqual(config_module.load_config(self.path).checkouts[0].alias, "a")
        with self.assertRaises(ScaffoldError):
            config_module.upsert_checkout(self.path, "/x/src/brave", alias="other")

    AWKWARD = ("/work/owner's checkout/src/brave", '/work/say "hi"/src/brave', "/work/back\\slash/src/brave",
               "/work/tab\there/src/brave", "/work/café ☕/src/brave", "/work/new\nline/src/brave")

    def test_awkward_paths_and_directories_round_trip_through_generated_records(self):
        for index, core in enumerate(self.AWKWARD):
            with self.subTest(core=core):
                path = self.dir / ("case-%d.toml" % index)
                alias = "checkout-%d" % index
                self.assertEqual(config_module.upsert_checkout(path, core, alias=alias), "added")
                self.assertEqual(config_module.upsert_checkout(path, core, direnv_dir="env's \"dir\""), "updated")
                self.assertEqual(config_module.upsert_checkout(path, core, alias=alias), "unchanged")
                record = config_module.load_config(path).checkouts[0]
                self.assertEqual((record.alias, str(record.core), record.direnv_dir_text),
                                 (alias, core, "env's \"dir\""))
                with self.assertRaises(ScaffoldError):
                    config_module.upsert_checkout(path, core, alias=alias + "x")

    def test_a_file_that_would_not_parse_is_not_replaced(self):
        broken = 'schema_version = 1\nlogging = [\n[[checkouts]]\ncore = "/x/src/brave"\n'
        self.path.write_text(broken)
        with self.assertRaises(ScaffoldError) as caught:
            config_module.upsert_checkout(self.path, "/x/src/brave", alias="a")
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertEqual(self.path.read_text(), broken)


if __name__ == "__main__":
    unittest.main()
