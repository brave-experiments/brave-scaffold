# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The list of repositories Core patches is read the way Core reads it."""

import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import patch_inventory
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


class SpecialFileTests(unittest.TestCase):
    """Hashing a file named by a patch must never wait on a named pipe or read a device."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def within_a_deadline(self, function, seconds=5):
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(function()), daemon=True)
        worker.start()
        worker.join(seconds)
        return outcome if outcome else None

    def unblock(self, path):
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass

    def test_a_regular_file_is_hashed_and_a_named_pipe_is_not_waited_on(self):
        regular = self.dir / "file.txt"
        regular.write_text("hello\n")
        self.assertEqual(patch_inventory.sha256_or_none(regular), hashlib.sha256(b"hello\n").hexdigest())
        pipe = self.dir / "pipe"
        os.mkfifo(pipe)
        self.addCleanup(self.unblock, pipe)
        self.assertEqual(self.within_a_deadline(lambda: patch_inventory.sha256_or_none(pipe)), [None],
                         "a pipe has no content to hash and opening it blocks")
        link = self.dir / "link-to-pipe"
        link.symlink_to(pipe)
        self.assertEqual(self.within_a_deadline(lambda: patch_inventory.sha256_or_none(link)), [None])

    def test_devices_and_missing_files_have_no_hash(self):
        self.assertIsNone(patch_inventory.sha256_or_none("/dev/null"))
        self.assertIsNone(patch_inventory.sha256_or_none(self.dir / "absent"))
        with self.assertRaises(OSError):
            patch_inventory.sha256_file("/dev/null")


if __name__ == "__main__":
    unittest.main()
