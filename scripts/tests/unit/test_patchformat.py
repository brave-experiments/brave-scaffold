# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which files a patch writes, including patches whose body lines look like file headers."""

import unittest

import tests.support  # noqa: F401
from scaffold.brave.patchformat import UnknownPatchFormat, parse_patch_targets


def patch(*lines, header="diff --git a/base/BUILD.gn b/base/BUILD.gn\n--- a/base/BUILD.gn\n+++ b/base/BUILD.gn\n"):
    return header + "\n".join(lines) + "\n"


class ParsePatchTargetsTests(unittest.TestCase):
    def test_reads_git_and_plain_unified_headers(self):
        self.assertEqual(parse_patch_targets(patch("@@ -1 +1 @@", "-old", "+new")), {"base/BUILD.gn"})
        plain = "--- a/one.cc\n+++ b/one.cc\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertEqual(parse_patch_targets(plain), {"one.cc"})

    def test_reads_renames_and_new_files(self):
        text = ("diff --git a/old.cc b/new.cc\nsimilarity index 90%\nrename from old.cc\nrename to new.cc\n"
                "diff --git a/added.cc b/added.cc\n--- /dev/null\n+++ b/added.cc\n@@ -0,0 +1 @@\n+x\n")
        self.assertEqual(parse_patch_targets(text), {"old.cc", "new.cc", "added.cc"})

    def test_a_removed_line_that_starts_with_two_dashes_is_not_a_header(self):
        text = patch("@@ -1,3 +1,2 @@", " keep", "--- comment", " tail")
        self.assertEqual(parse_patch_targets(text), {"base/BUILD.gn"})

    def test_an_added_line_that_starts_with_two_pluses_is_not_a_header(self):
        text = patch("@@ -1,2 +1,3 @@", " keep", "+++ docs/x y", " tail")
        self.assertEqual(parse_patch_targets(text), {"base/BUILD.gn"})

    def test_headers_after_a_hunk_still_count(self):
        text = (patch("@@ -1 +1 @@", "--- comment", "+++ added", header="diff --git a/a.cc b/a.cc\n--- a/a.cc\n+++ b/a.cc\n")
                + "diff --git a/b.cc b/b.cc\n--- a/b.cc\n+++ b/b.cc\n@@ -1 +1 @@\n-x\n+y\n")
        self.assertEqual(parse_patch_targets(text), {"a.cc", "b.cc"})

    def test_no_newline_markers_do_not_end_a_hunk_early(self):
        text = patch("@@ -1,2 +1 @@", "-one", "\\ No newline at end of file", "--- two",
                     "+uno", "\\ No newline at end of file")
        self.assertEqual(parse_patch_targets(text), {"base/BUILD.gn"})

    def test_input_without_file_headers_is_refused(self):
        with self.assertRaises(UnknownPatchFormat):
            parse_patch_targets("just text\n")


if __name__ == "__main__":
    unittest.main()
