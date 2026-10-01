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


class NonCompilingModeTests(unittest.TestCase):
    def test_options_that_skip_compilation_are_recognised_with_their_values(self):
        self.assertEqual(buildopts.interpret(["--prepare_only"]).skips_compilation, "--prepare_only")
        for tokens in (["--xcode_gen", "ios"], ["--xcode_gen=ios"]):
            found = buildopts.interpret(tokens + ["Release"])
            self.assertEqual((found.skips_compilation, found.build_config), ("--xcode_gen", "Release"), tokens)
        self.assertIsNone(buildopts.interpret(["--force_gn_gen", "Debug"]).skips_compilation)

    def test_ninja_options_that_do_not_build_are_recognised_in_every_accepted_form(self):
        for tokens in (["--ninja=n:"], ["--ninja", "n:"], ["--ninja=n"], ["--ninja", "t:targets"], ["--ninja=h:"]):
            with self.subTest(tokens=tokens):
                self.assertIsNotNone(buildopts.interpret(tokens).skips_compilation)
        for tokens in (["--ninja=j:8"], ["--ninja", "d:stats"], ["--ninja=k:0"]):
            with self.subTest(tokens=tokens):
                self.assertIsNone(buildopts.interpret(tokens).skips_compilation)

    def test_ninja_directory_and_build_file_options_do_not_verify_a_default_artifact(self):
        for tokens in (["--ninja=C:/absolute/other"], ["--ninja", "C:relative"],
                       ["--ninja=f:alternate.ninja"], ["--ninja", "f:alternate.ninja"]):
            with self.subTest(tokens=tokens):
                effective = resolve(tokens)
                self.assertTrue(effective.unresolved)
                if "C:" in " ".join(tokens):
                    self.assertIsNone(effective.output_dir)
                else:
                    self.assertTrue(effective.changes_output)

    def test_a_dry_run_leaves_the_output_alone_but_a_ninja_tool_may_not(self):
        self.assertFalse(resolve(["--ninja=n:"]).changes_output)
        self.assertTrue(resolve(["--ninja", "t:clean"]).changes_output)
        self.assertEqual(len(resolve(["--ninja=n:"]).unresolved), 1)

    def test_gn_overrides_of_the_target_and_architecture_are_reconciled_with_the_build_identity(self):
        self.assertEqual(resolve(["--gn=target_cpu:arm64"]).unresolved, [])
        self.assertEqual(resolve(["--gn", 'target_cpu:"arm64"', "--target_arch=arm64"]).unresolved, [])
        self.assertEqual(len(resolve(["--gn=target_cpu:x64"]).unresolved), 1)
        self.assertEqual(len(resolve(["--gn=target_os:android"]).unresolved), 1)
        self.assertEqual(resolve(["--gn=target_cpu:x64", "--target_arch=x64"]).unresolved, [])
        with self.assertRaises(ScaffoldError) as caught:
            resolve(["--gn=target_os:mac"], target="android", explicit_target="android")
        self.assertEqual(caught.exception.code, "SELECTOR_CONFLICT")

    def test_only_prepare_only_leaves_the_output_directory_alone(self):
        self.assertFalse(resolve(["--prepare_only"]).changes_output)
        self.assertTrue(resolve(["--xcode_gen=ios"]).changes_output)
        self.assertTrue(resolve([]).changes_output)
        self.assertEqual(len(resolve(["--prepare_only"]).unresolved), 1)


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

    def test_android_adds_its_generated_arguments_unless_forwarded(self):
        effective = resolve([], target="android")
        self.assertIn("--target_android_output_format=apk", effective.generated)
        self.assertIn("--gn=use_mold:false", effective.generated)
        forwarded = resolve(["--gn", "use_mold:true", "--gn=is_component_build:true"], target="android")
        self.assertNotIn("--gn=use_mold:false", forwarded.generated)
        self.assertNotIn("--gn=is_component_build:false", forwarded.generated)
        self.assertIn("--gn=enable_android_secondary_abi:false", forwarded.generated)
        self.assertTrue(resolve(["--target_android_output_format=aab"], target="android").unresolved)
        self.assertNotIn("--gn=use_mold:false", resolve([]).generated, "macOS is unchanged")

    def test_unresolvable_builds_are_flagged(self):
        for tokens in (["--help"], ["--target", "brave_unit_tests"], ["-C"]):
            self.assertTrue(resolve(tokens).unresolved, tokens)
        self.assertEqual(resolve(["--target", "brave"]).unresolved, [])


if __name__ == "__main__":
    unittest.main()
