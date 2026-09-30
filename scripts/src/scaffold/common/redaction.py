# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""One redaction policy for command logs, results, plans, errors, and saved records."""

from __future__ import annotations

import re

SECRET_NAME = re.compile(
    r"(token|secret|passw(?:or)?d|api[-_]?key|auth(?:orization)?|credential|private[-_]?key)",
    re.IGNORECASE)
URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/@\s:]+):(?P<secret>[^/@\s]+)@",
                             re.IGNORECASE)
REDACTED = "***"


def redact_url_credentials(text):
    return URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:" + REDACTED + "@", text)


def redact_argv(argv):
    """Hide secret values in separated and --key=value forms and in URLs."""
    result = []
    hide_next = False
    for part in argv:
        part = str(part)
        if hide_next:
            result.append(REDACTED)
            hide_next = False
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
