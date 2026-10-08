# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Argument parsing rules that every command shares."""

import unittest

import tests.support  # noqa: F401  (puts the source tree on sys.path)
from scaffold.brave.bcore import parse_command, resolve_command
from scaffold.common.cli import CommandSpec, Opt, Positional, parse_leading, parse_tokens
from scaffold.common.results import ScaffoldError

FORWARDING = CommandSpec("build", "build", positionals=(Positional("target"),), forward=True,
                         options=(Opt("--filter", "filter"),))
STRICT = CommandSpec("context", "context")


class ForwardingParserTests(unittest.TestCase):
    def test_target_before_unknown_option_is_the_target(self):
        parsed = parse_tokens(FORWARDING, ["mac", "--upstream-option=value"])
        self.assertEqual((parsed.positionals, parsed.forwarded), (["mac"], ["--upstream-option=value"]))

    def test_positional_after_unknown_option_is_forwarded_not_the_target(self):
        parsed = parse_tokens(FORWARDING, ["--upstream-option", "android"])
        self.assertEqual((parsed.positionals, parsed.forwarded), ([], ["--upstream-option", "android"]))

    def test_extra_positionals_after_the_required_ones_are_forwarded_in_order(self):
        parsed = parse_tokens(FORWARDING, ["mac", "extra", "--x", "1", "more"])
        self.assertEqual((parsed.positionals, parsed.forwarded), (["mac"], ["extra", "--x", "1", "more"]))

    def test_known_options_are_removed_wherever_they_appear_before_the_delimiter(self):
        parsed = parse_tokens(FORWARDING, ["--json", "--unknown", "--checkout=main", "tail", "--filter", "A.*"])
        self.assertEqual(parsed.forwarded, ["--unknown", "tail"])
        self.assertEqual(parsed.values, {"json": True, "checkout": "main", "filter": "A.*"})

    def test_delimiter_is_consumed_once_and_the_rest_is_literal(self):
        parsed = parse_tokens(FORWARDING, ["mac", "--", "--json", "--checkout", "x", "--", "android"])
        self.assertEqual(parsed.forwarded, ["--json", "--checkout", "x", "--", "android"])
        self.assertEqual(parsed.values, {})

    def test_missing_and_invalid_known_option_values_are_scaffold_errors(self):
        for tokens in (["--filter"], ["--filter", "--json"], ["--format", "yaml"], ["--checkout", "--"]):
            with self.subTest(tokens=tokens), self.assertRaises(ScaffoldError) as caught:
                parse_tokens(FORWARDING, tokens)
            self.assertEqual(caught.exception.code, "INVALID_INPUT")

    def test_notify_takes_a_following_policy_word_but_not_other_words(self):
        spec = CommandSpec("build", "build", positionals=(Positional("target"),), forward=True,
                           options=(Opt("--notify", "notify", choices=("always", "major", "never"),
                                        optional_value=True, metavar="POLICY"),))
        for tokens, expected in ((["--notify", "never"], "never"), (["--notify", "NEVER"], "never"),
                                 (["mac", "--notify", "major"], "major"), (["--notify=never"], "never"),
                                 (["--notify"], "always")):
            with self.subTest(tokens=tokens):
                parsed = parse_tokens(spec, tokens)
                self.assertEqual(parsed.get("notify"), expected)
                self.assertEqual(parsed.forwarded, [])
        parsed = parse_tokens(spec, ["--notify", "mac"])
        self.assertEqual((parsed.get("notify"), parsed.positionals), ("always", ["mac"]))
        parsed = parse_tokens(spec, ["--", "--notify", "never"])
        self.assertEqual((parsed.get("notify"), parsed.forwarded), (None, ["--notify", "never"]))

    def test_a_repeatable_option_collects_every_value_in_order(self):
        spec = CommandSpec("sync-build", "x", positionals=(Positional("target"),), forward=True,
                           options=(Opt("--sync-arg", "sync_arg", metavar="ARG", repeatable=True),))
        parsed = parse_tokens(spec, ["mac", "--sync-arg=--force", "--sync-arg", "-D", "--tail", "--sync-arg=--target_os=ios"])
        self.assertEqual(parsed.get("sync_arg"), ["--force", "-D", "--target_os=ios"])
        self.assertEqual((parsed.positionals, parsed.forwarded), (["mac"], ["--tail"]))
        self.assertIsNone(parse_tokens(spec, ["mac"]).get("sync_arg"))
        with self.assertRaises(ScaffoldError):
            parse_tokens(spec, ["--sync-arg"])
        parsed = parse_tokens(spec, ["--", "--sync-arg=--force"])
        self.assertEqual((parsed.get("sync_arg"), parsed.forwarded), (None, ["--sync-arg=--force"]))

    def test_conflicting_repeated_values_fail_and_equal_ones_are_fine(self):
        with self.assertRaises(ScaffoldError) as caught:
            parse_tokens(FORWARDING, ["--checkout", "a", "--checkout=b"])
        self.assertEqual(caught.exception.code, "SELECTOR_CONFLICT")
        self.assertEqual(parse_tokens(FORWARDING, ["--checkout", "a", "--checkout=a"]).values, {"checkout": "a"})

    def test_abbreviations_are_not_accepted(self):
        parsed = parse_tokens(FORWARDING, ["--check", "main"])
        self.assertEqual(parsed.forwarded, ["--check", "main"])
        with self.assertRaises(ScaffoldError):
            parse_tokens(STRICT, ["--check", "main"])

    def test_commands_without_forwarding_reject_extras_with_suggestions(self):
        with self.assertRaises(ScaffoldError) as caught:
            parse_tokens(STRICT, ["--chekout", "x"])
        self.assertIn("--checkout", caught.exception.details["did_you_mean"])
        with self.assertRaises(ScaffoldError):
            parse_tokens(STRICT, ["extra"])

    def test_a_rejected_positional_is_echoed_without_a_secret_assignment(self):
        with self.assertRaises(ScaffoldError) as caught:
            parse_tokens(STRICT, ["API_TOKEN=hunter2"])
        self.assertNotIn("hunter2", caught.exception.message)

    def test_values_are_kept_exactly(self):
        tokens = ["", "a b", "-5", "café", "$(x)", "--key=v w"]
        parsed = parse_tokens(FORWARDING, ["mac", "--", *tokens])
        self.assertEqual(parsed.forwarded, tokens)


class CommandDiscoveryTests(unittest.TestCase):
    def test_notification_policy_before_command_or_subcommand(self):
        for policy in ("always", "major", "never", "NEVER"):
            for tokens, name, positionals in (
                    (["--notify", policy, "build", "mac"], "build", ["mac"]),
                    (["--notify", policy, "env", "check"], "env check", []),
                    (["env", "--notify", policy, "check"], "env check", [])):
                with self.subTest(tokens=tokens):
                    spec, remaining = resolve_command(tokens)
                    parsed = parse_command(spec, remaining)
                    self.assertEqual(spec.name, name)
                    self.assertEqual(parsed.get("notify"), policy.lower())
                    self.assertEqual(parsed.positionals, positionals)
                    self.assertEqual(parsed.forwarded, [])

    def test_bare_and_inline_notification_options_preserve_command_words(self):
        for tokens, name, policy in (
                (["--notify", "build", "mac"], "build", "always"),
                (["env", "--notify", "check"], "env check", "always"),
                (["--notify=never", "build", "mac"], "build", "never")):
            with self.subTest(tokens=tokens):
                spec, remaining = resolve_command(tokens)
                parsed = parse_command(spec, remaining)
                self.assertEqual(spec.name, name)
                self.assertEqual(parsed.get("notify"), policy)
                self.assertEqual(parsed.forwarded, [])


class LeadingParserTests(unittest.TestCase):
    SPEC = CommandSpec("bpm", "bpm", forward=True, leading_only=True)

    def test_scaffold_flags_only_before_the_first_other_token(self):
        parsed = parse_leading(self.SPEC, ["--json", "--checkout", "a", "run", "--json", "--checkout", "b"])
        self.assertEqual(parsed.values, {"json": True, "checkout": "a"})
        self.assertEqual(parsed.forwarded, ["run", "--json", "--checkout", "b"])

    def test_leading_delimiter_protects_scaffold_lookalikes(self):
        parsed = parse_leading(self.SPEC, ["--", "--help"])
        self.assertEqual((parsed.values, parsed.forwarded), ({}, ["--help"]))

    def test_unknown_leading_option_starts_the_package_arguments(self):
        parsed = parse_leading(self.SPEC, ["--version", "--json"])
        self.assertEqual(parsed.forwarded, ["--version", "--json"])


if __name__ == "__main__":
    unittest.main()
