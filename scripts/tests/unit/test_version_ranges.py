# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The npm range forms a checkout can declare for Node and its package manager."""

import unittest

from tests.support import SCRIPTS  # noqa: F401  (puts scaffold on sys.path)
from scaffold.common.tools import satisfies


class SatisfiesTests(unittest.TestCase):
    def check(self, version, range_text, expected):
        with self.subTest(version=version, range=range_text):
            self.assertIs(satisfies(version, range_text), expected)

    def test_comparators_with_and_without_spaces(self):
        for range_text in (">=20", ">= 20", ">=20.0.0", ">= 20.0.0", ">=v20"):
            self.check("22.1.0", range_text, True)
            self.check("19.9.0", range_text, False)
        self.check("20.1.0", ">20", False)
        self.check("21.0.0", ">20", True)
        self.check("20.9.9", "<=20", True)
        self.check("21.0.0", "<=20", False)
        self.check("19.9.9", "<20", True)
        self.check("20.0.0", "<20", False)
        self.check("20.1.0", ">=18 <21", True)
        self.check("21.0.0", ">= 18 < 21", False)

    def test_partial_versions_are_ranges(self):
        self.check("20.0.0", "20", True)
        self.check("20.5.1", "=20", True)
        self.check("21.0.0", "20", False)
        self.check("20.5.1", "20.5", True)
        self.check("20.6.0", "20.5", False)
        self.check("20.5.1", "20.5.1", True)
        self.check("20.5.2", "20.5.1", False)

    def test_wildcards(self):
        for wildcard in ("*", "x", "X", ""):
            self.check("1.2.3", wildcard, True)
        self.check("20.9.0", "20.x", True)
        self.check("21.0.0", "20.x", False)
        self.check("20.5.9", "20.5.x", True)
        self.check("20.6.0", "20.5.*", False)
        self.check("5.0.0", ">=*", True)

    def test_tilde(self):
        self.check("20.5.0", "~20", True)
        self.check("21.0.0", "~20", False)
        self.check("1.5.0", "~1", True)
        self.check("2.0.0", "~1", False)
        self.check("1.2.9", "~1.2", True)
        self.check("1.3.0", "~1.2", False)
        self.check("1.2.9", "~1.2.3", True)
        self.check("1.2.2", "~1.2.3", False)
        self.check("1.3.0", "~ 1.2.3", False)

    def test_caret(self):
        self.check("20.9.0", "^20", True)
        self.check("21.0.0", "^20", False)
        self.check("1.9.0", "^1.2.3", True)
        self.check("2.0.0", "^1.2.3", False)
        self.check("0.2.9", "^0.2.3", True)
        self.check("0.3.0", "^0.2.3", False)
        self.check("0.0.3", "^0.0.3", True)
        self.check("0.0.4", "^0.0.3", False)
        self.check("0.9.0", "^0", True)
        self.check("1.0.0", "^0", False)
        self.check("0.0.9", "^0.0", True)
        self.check("0.1.0", "^0.0", False)

    def test_alternatives_and_hyphen_ranges(self):
        self.check("18.5.0", "^18 || ^20", True)
        self.check("19.0.0", "^18 || ^20", False)
        self.check("20.1.0", "18 - 20", True)
        self.check("21.0.0", "18 - 20", False)
        self.check("17.9.9", "18 - 20", False)
        self.check("20.1.5", "18.2.0 - 20.1", True)
        self.check("20.2.0", "18.2.0 - 20.1", False)

    def test_prefixed_versions(self):
        self.check("v22.1.0", ">=20", True)
        self.check("22.1.0", ">=v20", True)

    def test_unsupported_forms_stay_unknown(self):
        self.check("22.1.0", ">=20-beta", None)
        self.check("22.1.0", "latest", None)
        self.check("not-a-version", ">=20", None)


if __name__ == "__main__":
    unittest.main()
