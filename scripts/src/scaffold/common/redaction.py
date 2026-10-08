# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""One redaction policy for command logs, results, plans, errors, and saved records."""

from __future__ import annotations

import re

# "auth" must not match author, authority, or authentic*.
SECRET_NAME = re.compile(
    r"(token|secret|passw(?:or)?d|api[-_]?key|auth(?!or(?!ization)|entic)(?:orization)?|credential|private[-_]?key)",
    re.IGNORECASE)
# The password runs to the last "@" before the path, so a password may contain "@".
URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/@\s:]+):(?P<secret>[^/\s]+)@",
                             re.IGNORECASE)
SECRET_HEADER = re.compile(
    r"^(?P<name>[A-Za-z0-9-]*(?:authorization|token|secret|api[-_]?key|password|credential|cookie)[A-Za-z0-9-]*)"
    r"\s*:\s*\S.*$", re.IGNORECASE)
REDACTED = "***"


def redact_url_credentials(text):
    return URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:" + REDACTED + "@", text)


def _attached_header(part):
    """(option prefix, header text) when an option carries its value in the same argument.

    `--header=Name: value` and `-HName: value` both do; curl and similar tools accept either form.
    """
    if part.startswith("--") and "=" in part:
        prefix, _, text = part.partition("=")
        return prefix + "=", text
    if part.startswith("-") and not part.startswith("--") and len(part) > 2:
        return part[:2], part[2:]
    return None


def header_secret_values(argument):
    """The secret values of a header carried by one argument, whole or attached to an option; else an empty set."""
    candidates = [argument]
    attached = _attached_header(argument)
    if attached:
        candidates.append(attached[1])
    found = set()
    for text in candidates:
        if SECRET_HEADER.match(text):
            value = text.partition(":")[2].strip()
            # Tools echo the whole header value, or only the credential after a scheme such as "Bearer".
            found.update({value, value.split()[-1]})
    return found


MIN_SCRUBBED_SECRET_LENGTH = 6  # shorter values would replace ordinary words; command lines are redacted by name anyway


def secret_values(argv, env):
    """The secret values a command was given, longest first: secret-named variables, arguments, URL passwords, headers."""
    secrets = {value for name, value in env.items() if value and SECRET_NAME.search(name)}
    hide_next = False
    for argument in map(str, argv):
        if hide_next:
            secrets.add(argument)
        hide_next = argument.startswith("-") and "=" not in argument and bool(SECRET_NAME.search(argument))
        if "=" in argument and SECRET_NAME.search(argument.partition("=")[0]):
            secrets.add(argument.partition("=")[2])
        secrets.update(match.group("secret") for match in URL_CREDENTIALS.finditer(argument))
        secrets.update(header_secret_values(argument))
    secrets.update(line for value in list(secrets) for line in value.splitlines() if line)
    return sorted((value for value in secrets if len(value) >= MIN_SCRUBBED_SECRET_LENGTH), key=len, reverse=True)


def scrub_secrets(text, secrets):
    """`text` with each known secret value, and any URL password, replaced."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return redact_url_credentials(text)


def redact_argv(argv):
    """Hide secret values in separated and --key=value forms, in headers, and in URLs."""
    result = []
    hide_next = False
    for part in argv:
        part = str(part)
        if hide_next:
            result.append(REDACTED)
            hide_next = False
            continue
        attached = _attached_header(part)
        attached_header = SECRET_HEADER.match(attached[1]) if attached else None
        if attached_header:
            result.append("%s%s: %s" % (attached[0], attached_header.group("name"), REDACTED))
            continue
        if part.startswith("-") and "=" in part:
            name, _, value = part.partition("=")
            if SECRET_NAME.search(name):
                result.append("%s=%s" % (name, REDACTED))
                continue
        elif part.startswith("-") and SECRET_NAME.search(part):
            result.append(part)
            hide_next = True
            continue
        elif "=" in part and not part.startswith("-") and SECRET_NAME.search(part.partition("=")[0]) \
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part.partition("=")[0]):
            result.append("%s=%s" % (part.partition("=")[0], REDACTED))
            continue
        header = SECRET_HEADER.match(part)
        if header:
            result.append("%s: %s" % (header.group("name"), REDACTED))
            continue
        result.append(redact_url_credentials(part))
    return result


class Arguments(list):
    """A command line or part of one; a report always redacts it as arguments, whatever field holds it."""


def _holds_arguments(key):
    return key == "arguments" or key == "argv" or str(key).endswith(("_argv", "_arguments"))


def redact_report(value, key=None):
    """A copy of a result or record structure that is safe to show or store.

    `Arguments` values, and lists stored under argument-like keys (`argv`, `arguments`, `*_argv`,
    `*_arguments`), are redacted as command lines; every other string only loses credentials embedded
    in URLs. Diagnostics that echo user arguments should use `Arguments` so a field name is not the
    only protection.
    """
    if isinstance(value, dict):
        return {name: redact_report(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        if (isinstance(value, Arguments) or _holds_arguments(key)) and all(isinstance(item, str) for item in value):
            return redact_argv(value)
        return [redact_report(item, key) for item in value]
    if isinstance(value, str):
        return redact_url_credentials(value)
    return value
