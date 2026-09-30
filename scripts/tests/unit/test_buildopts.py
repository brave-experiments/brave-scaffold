# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Interpretation of output-affecting options forwarded to the package build."""

import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import buildopts
from scaffold.common.results import ScaffoldError

SRC = Path("/work/browser/_bad_scm/workspace/src")


def resolve(tokens, target="mac", configuration="Debug", explicit_target=None, explicit_configuration=None,
            offline=False):
    return buildopts.resolve_effective(SRC, tokens, target, configuration, explicit_target, explicit_configuration,
                                       offline)


class InterpretTests(unittest.TestCase):
    def test_output_directory_forms_and_last_value_wins(self):
        self.assertEqual(buildopts.interpret(["-C", "One"]).build_dir, "One")
        self.assertEqual(buildopts.interpret(["-CTwo"]).build_dir, "Two")
        self.assertEqual(buildopts.interpret(["-C", "One", "-C", "Three"]).build_dir, "Three")
        self.assertEqual(buildopts.interpret(["-C"]).problems, ["-C has no value"])

    def test_options_with_values_do_not_leak_into_configuration(self):
        found = buildopts.interpret(["--channel", "release", "--target_arch=x64", "--target", "brave"])
        self.assertEqual((found.channel, found.target_arch, found.target, found.build_config),
                         ("release", "x64", "brave", None))
        self.assertEqual(buildopts.interpret(["Release"]).build_config, "Release")

    def test_remote_execution_forms(self):
        self.assertIs(buildopts.interpret(["--use_remoteexec"]).remoteexec, True)
        self.assertIs(buildopts.interpret(["--use_remoteexec", "false"]).remoteexec, False)
        self.assertIs(buildopts.interpret(["--use_remoteexec=false"]).remoteexec, False)
        self.assertTrue(buildopts.interpret(["--offline"]).offline)


class EffectiveTests(unittest.TestCase):
    def test_defaults_match_the_package_command_convention(self):
        effective = resolve([])
        self.assertEqual(effective.output_dir, SRC / "out" / "Debug_arm64")
        self.assertEqual(effective.generated, ["--target_os=mac", "--target_arch=arm64", "-C", "Debug_arm64",
                                               "Debug", "--use_remoteexec=true"])
        self.assertEqual(resolve([], target="android").output_dir, SRC / "out" / "android_Debug_arm64")
        self.assertEqual(resolve(["--target_arch=x64"]).output_dir, SRC / "out" / "Debug")

    def test_relative_output_is_beneath_src_out_and_absolute_is_kept(self):
        self.assertEqual(resolve(["-C", "Custom"]).output_dir, SRC / "out" / "Custom")
        self.assertEqual(resolve(["-C", "/abs/out"]).output_dir, Path("/abs/out"))
        self.assertNotIn("-C", resolve(["-C", "Custom"]).generated)

    def test_forwarded_choices_replace_generated_defaults(self):
        effective = resolve(["Release", "--offline"])
        self.assertEqual(effective.configuration, "Release")
        self.assertNotIn("Debug", effective.generated)
        self.assertNotIn("--use_remoteexec=true", effective.generated)
        self.assertIn("--channel=release", effective.generated)

    def test_explicit_scaffold_choices_conflict_with_different_forwarded_ones(self):
        for kwargs, tokens in (({"explicit_configuration": "Release"}, ["Debug"]),
                               ({"explicit_target": "android", "target": "android"}, ["--target_os=mac"]),
                               ({"offline": True}, ["--use_remoteexec=true"])):
            with self.subTest(tokens=tokens), self.assertRaises(ScaffoldError) as caught:
                resolve(tokens, **kwargs)
            self.assertEqual(caught.exception.code, "SELECTOR_CONFLICT")
        resolve(["Debug"], explicit_configuration="Debug")

    def test_unresolvable_builds_are_flagged(self):
        for tokens in (["--help"], ["--target", "brave_unit_tests"], ["-C"]):
            self.assertTrue(resolve(tokens).unresolved, tokens)
        self.assertEqual(resolve(["--target", "brave"]).unresolved, [])


if __name__ == "__main__":
    unittest.main()
