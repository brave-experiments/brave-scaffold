# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The `bcore` command line."""

from __future__ import annotations

import difflib
import sys

from ..common.cli import (COMMON_OPTIONS, Parsed, check_positionals, detect_json, help_requested, parse_leading,
                          parse_tokens, render_help)
from ..common.revision import format_revision, read_revision
from ..common.results import ScaffoldError
from ..common.procs import install_signal_handlers
from .app import run_command
from .registry import GROUPS, REGISTRY

VALUE_FLAGS = {option.name for option in COMMON_OPTIONS if option.takes_value and not option.optional_value}


def _pop_word(tokens):
    """Remove and return the first non-flag word, skipping scaffold flags and their values."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None, tokens
        name = token.partition("=")[0] if token.startswith("--") else token
        if name in VALUE_FLAGS:
            index += 1 if "=" in token else 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[:index] + tokens[index + 1:]
    return None, tokens


def top_help():
    lines = ["Usage: bcore [options] <command> [arguments]", "",
             "Sync, build, test, run, and inspect Brave Core checkouts.",
             "Brave Scaffold is optional and requires no integration changes inside Core.", "", "Commands:"]
    seen = set()
    for name, spec in REGISTRY.items():
        if spec.name in seen:
            continue
        seen.add(spec.name)
        lines.append("  %-16s %s" % (spec.name, spec.summary))
    lines += ["", "Use bcore --version to show the scaffold Git revision."]
    lines += ["", "Options (before or after the command, up to '--'):"]
    lines += ["  %-28s %s" % ((o.name + ("[=%s]" if o.optional_value else " %s") % o.metavar) if o.takes_value else o.name,
                           o.help) for o in COMMON_OPTIONS]
    lines += ["", "Run 'bcore <command> --help' for a command's arguments, defaults, and side effects.",
              "Use one operator per checkout at a time; the scaffold does not lock checkouts."]
    return "\n".join(lines) + "\n"


def resolve_command(argv):
    tokens = list(argv)
    word, rest = _pop_word(tokens)
    if word is None:
        return None, tokens
    if word in GROUPS:
        sub, remaining = _pop_word(rest)
        if sub is None or sub not in GROUPS[word]:
            raise ScaffoldError("INVALID_INPUT", "%s needs a subcommand: %s." % (word, ", ".join(GROUPS[word])),
                                details={"usage": "bcore %s <%s>" % (word, "|".join(GROUPS[word]))})
        return REGISTRY["%s %s" % (word, sub)], remaining
    spec = REGISTRY.get(word)
    if spec is None:
        names = sorted({s.name for s in REGISTRY.values()} | set(GROUPS))
        details = {"commands": names}
        close = difflib.get_close_matches(word, names, n=3)
        if close:
            details["did_you_mean"] = close
        raise ScaffoldError("INVALID_INPUT", "Unknown command %r." % word, details=details)
    return spec, rest


def parse_command(spec, tokens):
    if spec.leading_only:
        return parse_leading(spec, tokens)
    parsed = parse_tokens(spec, tokens)
    if spec.post_parse:
        spec.post_parse(spec, parsed)
    else:
        check_positionals(spec, parsed)
    return parsed


def _wants_help(tokens):
    for token in tokens:
        if token == "--":
            return False
        if token in ("-h", "--help"):
            return True
    return False


def main(argv, stdout=None, stderr=None, notifier=None, bell=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if argv == ["--version"]:
        stdout.write(format_revision(read_revision()) + "\n")
        return 0
    install_signal_handlers()
    json_mode = detect_json(argv)
    try:
        spec, tokens = resolve_command(argv)
        if spec is None:
            if _wants_help(argv) or not argv:
                stdout.write(top_help())
                return 0 if argv else 2
            raise ScaffoldError("INVALID_INPUT", "No command was given.", details={"usage": "bcore <command>"})
        if help_requested(spec, tokens):
            stdout.write(render_help(spec))
            return 0
        parsed = parse_command(spec, tokens)
    except ScaffoldError as error:
        failure = Parsed(values={"json": json_mode})

        def raise_error(_context):
            raise error
        command = " ".join(argv[:1]) if argv and not argv[0].startswith("-") else "bcore"
        return run_command(command, failure, raise_error, needs_config=False, stdout=stdout, stderr=stderr,
                           notify=False)
    return run_command(spec.name, parsed, spec.handler,
                       needs_config=spec.name not in ("capabilities",), stdout=stdout, stderr=stderr,
                       may_create_config=spec.creates_config, notifier=notifier, bell=bell)
