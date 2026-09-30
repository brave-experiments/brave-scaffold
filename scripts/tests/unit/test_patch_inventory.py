# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The list of repositories Core patches is read the way Core reads it."""

import unittest

import tests.support  # noqa: F401
from scaffold.brave.patch_inventory import PatchRepository, parse_repositories


class RepositoryListTests(unittest.TestCase):
    def test_comments_blank_lines_and_trailing_slashes_are_ignored(self):
        text = "# Chromium\n//\n\n//v8  # engine\n//third_party/ffmpeg/\n"
        self.assertEqual(parse_repositories(text), ["", "v8", "third_party/ffmpeg"])

    def test_malformed_lists_are_rejected(self):
        for text in ("v8\n//\n", "//\n//v8\n//v8\n", "//\n//../outside\n", "//v8\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_repositories(text)

    def test_source_paths_are_relative_to_chromiums_source_root(self):
        root = PatchRepository("", None, None)
        nested = PatchRepository("third_party/ffmpeg", None, None)
        self.assertEqual((root.source_path("base/BUILD.gn"), nested.source_path("config.h")),
                         ("base/BUILD.gn", "third_party/ffmpeg/config.h"))


if __name__ == "__main__":
    unittest.main()
