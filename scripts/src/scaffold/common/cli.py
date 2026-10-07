# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Argument parsing with scaffold options and unknown-argument forwarding."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from .redaction import Arguments, redact_argv
from .results import ScaffoldError


@dataclass(frozen=True)
class Opt:
    name: str
    dest: str
    takes_value: bool = True
    choices: tuple = ()
    help: str = ""
    metavar: str = "VALUE"
    optional_value: bool = False
    repeatable: bool = False


COMMON_OPTIONS = (
    Opt("--quiet", "quiet", takes_value=False, help="Hide progress and child output; show a failure excerpt."),
    Opt("--verbose", "verbose", takes_value=False, help="Also show internal commands and their directories."),
    Opt("--verbosity", "verbosity", choices=("quiet", "normal", "verbose"),
        help="Console detail; overrides configuration.", metavar="LEVEL"),
    Opt("--checkout", "checkout", help="Checkout alias or path (Core, Chromium src, or outer checkout).",
        metavar="NAME_OR_PATH"),
    Opt("--config", "config", help="Configuration file (default: the installation's brave-scaffold.toml).",
        metavar="FILE"),
    Opt("--notify", "notify", choices=("always", "major", "never"), optional_value=True, metavar="POLICY",
        help="Desktop notification on completion: bare means always; or always, major, never."),
    Opt("--json", "json", takes_value=False, help="Print one JSON result document on stdout."),
    Opt("--format", "format", choices=("json", "text", "bash"),
        help="Output format; 'json' is an alias of --json.", metavar="FORMAT"),
)


@dataclass(frozen=True)
class Positional:
    name: str
    required: bool = False
    choices: tuple = ()
    help: str = ""


@dataclass
class CommandSpec:
    name: str
    summary: str
    handler: object = None
    aliases: tuple = ()
    positionals: tuple = ()
    options: tuple = ()
    forward: bool = False
    side_effects: str = "None (read-only)."
    examples: tuple = ()
    notes: str = ""
    common: tuple = COMMON_OPTIONS
    max_positionals: int | None = None
    leading_only: bool = False
    creates_config: bool = False
    post_parse: object = None

    @property
    def positional_limit(self):
        return len(self.positionals) if self.max_positionals is None else self.max_positionals


@dataclass
class Parsed:
    values: dict = field(default_factory=dict)
    positionals: list = field(default_factory=list)
    forwarded: list = field(default_factory=list)
    delimiter: bool = False

    def get(self, name, default=None):
        return self.values.get(name, default)

    @property
    def json_mode(self):
        return bool(self.values.get("json")) or self.values.get("format") == "json"


def usage_line(spec, prefix="bcore"):
    parts = [prefix] if spec.name == prefix else [prefix, spec.name]
    for positional in spec.positionals:
        parts.append("<%s>" % positional.name if positional.required else "[<%s>]" % positional.name)
    parts.append("[options]")
    if spec.forward:
        parts.append("[--] <arguments>..." if spec.leading_only else "[-- <arguments forwarded to the package command>]")
    return " ".join(parts)


def _input_error(message, spec=None, **details):
    if spec is not None:
        details.setdefault("usage", usage_line(spec))
    return ScaffoldError("INVALID_INPUT", message, details=details)


def parse_tokens(spec, tokens):
    """Parse tokens after the command name.

    Scaffold options may appear anywhere before `--`. Unknown options, and
    positionals after the positional prefix closes, are forwarded unchanged when
    the command forwards; otherwise they are reported.
    """
    options = {option.name: option for option in (*spec.options, *spec.common)}
    parsed = Parsed()
    prefix_closed = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            if tokens[index + 1:] and not spec.forward:
                raise _input_error("%s does not take arguments after '--'; nothing was run." % spec.name, spec,
                                   ignored=Arguments(tokens[index + 1:]))
            parsed.delimiter = True
            parsed.forwarded.extend(tokens[index + 1:])
            break
        if token.startswith("-") and len(token) > 1:
            name, equals, inline = token.partition("=") if token.startswith("--") else (token, "", "")
            option = options.get(name)
            if option is None:
                if spec.forward:
                    prefix_closed = True
                    parsed.forwarded.append(token)
                    index += 1
                    continue
                raise _unknown_option(spec, name, options)
            if option.optional_value and not equals:
                following = tokens[index + 1].lower() if index + 1 < len(tokens) else None
                if option.choices and following in option.choices:
                    index += 1
                    value = following
                else:
                    value = "always"
            elif option.takes_value:
                if equals:
                    value = inline
                else:
                    index += 1
                    if index >= len(tokens) or tokens[index] == "--" or tokens[index] in options:
                        raise _input_error("%s needs a value." % name, spec,
                                           example="%s %s=%s" % (usage_line(spec), name, option.metavar))
                    value = tokens[index]
                if option.choices and value.lower() not in option.choices:
                    raise _input_error("%s must be one of: %s." % (name, ", ".join(option.choices)), spec)
                value = value.lower() if option.choices else value
            else:
                if equals:
                    raise _input_error("%s does not take a value." % name, spec)
                value = True
            _set(parsed.values, option, value, spec)
            index += 1
            continue
        if not prefix_closed and len(parsed.positionals) < spec.positional_limit:
            parsed.positionals.append(token)
        elif spec.forward:
            prefix_closed = True
            parsed.forwarded.append(token)
        else:
            raise _input_error("Unexpected argument %r." % redact_argv([token])[0], spec)
        index += 1
    return parsed


def _set(values, option, value, spec):
    if option.repeatable:
        values.setdefault(option.dest, []).append(value)
        return
    if option.dest in ("quiet", "verbose"):
        value = option.dest
        option = Opt(option.name, "verbosity")
    if option.dest in values and values[option.dest] != value:
        raise ScaffoldError(
            "SELECTOR_CONFLICT",
            "%s was given twice with different values: %r and %r." % (option.name, values[option.dest], value),
            details={"option": option.name})
    values[option.dest] = value


def _unknown_option(spec, name, options):
    close = difflib.get_close_matches(name, list(options), n=3)
    details = {"valid_options": sorted(options)}
    if close:
        details["did_you_mean"] = close
    return _input_error("%s does not accept the option %s." % (spec.name, name), spec, **details)


def check_positionals(spec, parsed):
    """Enforce required positionals and choices after command-specific handling."""
    for index, positional in enumerate(spec.positionals):
        if index >= len(parsed.positionals):
            if positional.required:
                raise _input_error("Missing required argument <%s>." % positional.name, spec,
                                   example=(spec.examples[0] if spec.examples else None))
            continue
        value = parsed.positionals[index]
        if positional.choices and value.lower() not in positional.choices:
            raise _input_error("<%s> must be one of: %s." % (positional.name, ", ".join(positional.choices)), spec)


def detect_json(tokens):
    """Find a JSON request in the scaffold-option region of an argument list."""
    for index, token in enumerate(tokens):
        if token == "--":
            return False
        if token == "--json" or token == "--format=json":
            return True
        if token == "--format" and index + 1 < len(tokens) and tokens[index + 1] == "json":
            return True
    return False


def render_help(spec, prefix="bcore"):
    lines = ["Usage: " + usage_line(spec, prefix), "", spec.summary, ""]
    if spec.aliases:
        lines.append("Aliases: %s" % ", ".join(spec.aliases))
    for positional in spec.positionals:
        lines.append("  <%s>%s  %s" % (positional.name, " (required)" if positional.required else "", positional.help))
    lines.append("Options:")
    for option in (*spec.options, *spec.common):
        argument = ("%s[=%s]" % (option.name, option.metavar) if option.optional_value else
                    "%s %s" % (option.name, option.metavar)) if option.takes_value else option.name
        choices = " [%s]" % "|".join(option.choices) if option.choices else ""
        lines.append("  %-28s %s%s" % (argument, option.help, choices))
    lines.append("")
    if spec.leading_only:
        lines.append("Scaffold options are accepted only before the first %s argument." % (
            "package" if prefix == "bpm" else "program"))
    else:
        lines.append("Scaffold options may appear before or after the command, up to '--'.")
    if spec.forward and not spec.leading_only:
        lines.append("Unknown options and extra arguments go unchanged to the package command; "
                     "use '--' to forward a token that is also a scaffold option.")
    lines.append("Side effects: %s" % spec.side_effects)
    if spec.notes:
        lines.extend(["", spec.notes])
    if spec.examples:
        lines.append("")
        lines.append("Examples:")
        lines.extend("  %s" % example for example in spec.examples)
    return "\n".join(lines) + "\n"


def parse_leading(spec, tokens):
    """Parse scaffold options only before the first non-scaffold token.

    Everything from that token onward is forwarded verbatim, including tokens
    that look like scaffold options. A leading `--` ends scaffold options and is
    consumed.
    """
    options = {option.name: option for option in (*spec.options, *spec.common)}
    parsed = Parsed()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            parsed.delimiter = True
            index += 1
            break
        name, equals, inline = token.partition("=") if token.startswith("--") else (token, "", "")
        option = options.get(name)
        if option is None:
            break
        if option.optional_value and not equals:
            value = "always"
        elif option.takes_value:
            if equals:
                value = inline
            else:
                index += 1
                if index >= len(tokens) or tokens[index] == "--" or tokens[index] in options:
                    raise _input_error("%s needs a value." % name, spec)
                value = tokens[index]
            if option.choices and value.lower() not in option.choices:
                raise _input_error("%s must be one of: %s." % (name, ", ".join(option.choices)), spec)
            value = value.lower() if option.choices else value
        else:
            value = True
        _set(parsed.values, option, value, spec)
        index += 1
    parsed.forwarded = list(tokens[index:])
    return parsed


def help_requested(spec, tokens):
    """Whether help is asked for inside the command's own scaffold-option region.

    Ordinary commands take scaffold options anywhere before `--`. A direct-tool command
    (`bpm`, `vpython3`) takes them only before its first program argument, so `--help` after
    that belongs to the program.
    """
    known = {option.name: option for option in (*spec.options, *spec.common)}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-h", "--help"):
            return True
        if token == "--":
            return False
        if spec.leading_only:
            name = token.partition("=")[0] if token.startswith("--") else token
            if name not in known:
                return False
            index += 2 if known[name].takes_value and not known[name].optional_value and "=" not in token else 1
        else:
            index += 1
    return False


def detect_json_leading(spec, tokens):
    """JSON request among the leading scaffold options of a direct-tool command."""
    known = {option.name: option for option in (*spec.options, *spec.common)}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = token.partition("=")[0] if token.startswith("--") else token
        if name == "--json" or token == "--format=json":
            return True
        if name == "--format" and index + 1 < len(tokens) and tokens[index + 1] == "json":
            return True
        if name not in known:
            return False
        index += 2 if known[name].takes_value and not known[name].optional_value and "=" not in token else 1
    return False
