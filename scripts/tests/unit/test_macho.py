# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Mach-O load commands and whether an application bundle loads libraries from outside itself."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import fat_macho_bytes, macho_bytes
from scaffold.brave import macho, macos

FRAMEWORK = "Contents/Frameworks/Example Framework.framework/Versions/1.0"


class BundleTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "out" / "Debug_arm64"
        self.app = self.out / "Example.app"

    def write(self, relative, data):
        path = self.app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def executable(self, *dylibs, rpaths=("@executable_path/../Frameworks",)):
        self.write("Contents/MacOS/Example", macho_bytes(["/usr/lib/libSystem.B.dylib", *dylibs], rpaths,
                                                       filetype=macho.MH_EXECUTE))


class ReadImageTests(BundleTestCase):
    def test_dylib_references_and_run_paths_are_read_from_a_thin_file(self):
        path = self.write("thin", macho_bytes(["@rpath/libone.dylib", ("@rpath/libweak.dylib", 0x80000018)],
                                              ["@loader_path/../lib"]))
        image = macho.read_image(path)
        self.assertEqual(image.dylibs, [("@rpath/libone.dylib", "load"), ("@rpath/libweak.dylib", "weak")])
        self.assertEqual(image.rpaths, ["@loader_path/../lib"])

    def test_the_arm64_slice_of_a_universal_file_is_read(self):
        path = self.write("fat", fat_macho_bytes([(0x01000007, macho_bytes(["/opt/x86_only.dylib"], cputype=0x01000007)),
                                                  (0x0100000C, macho_bytes(["@rpath/libarm.dylib"]))]))
        self.assertEqual(macho.read_image(path).dylibs, [("@rpath/libarm.dylib", "load")])

    def test_the_slice_for_the_build_architecture_is_read(self):
        path = Path(self.temp.name) / "fat"
        path.write_bytes(fat_macho_bytes([(0x01000007, macho_bytes(["/opt/x86_only.dylib"], cputype=0x01000007)),
                                                  (0x0100000C, macho_bytes(["@rpath/libarm.dylib"]))]))
        self.assertEqual(macho.read_image(path, "x64").dylibs, [("/opt/x86_only.dylib", "load")])
        self.write("Contents/MacOS/Example", fat_macho_bytes([
            (0x01000007, macho_bytes(["/opt/x86_only.dylib"], filetype=macho.MH_EXECUTE, cputype=0x01000007)),
            (0x0100000C, macho_bytes(["/usr/lib/libSystem.B.dylib"], filetype=macho.MH_EXECUTE))]))
        self.assertEqual(macho.outside_references(self.app), [])
        self.assertEqual([item["dylib"] for item in macho.outside_references(self.app, "x64")], ["/opt/x86_only.dylib"])

    def test_other_files_are_not_images(self):
        for data in (b"", b"PK\x03\x04 not mach-o", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
                     b"\xca\xfe\xba\xbe\x00\x00\x00\x02\x01", b"\xca\xfe\xba\xbe\x00\x00\x00\x34 java class"):
            with self.subTest(data=data):
                self.assertIsNone(macho.read_image(self.write("file", data)))


class OutsideReferenceTests(BundleTestCase):
    def test_a_component_build_loads_libraries_from_the_output_directory(self):
        self.executable("@rpath/libbase.dylib", rpaths=("@loader_path/../../..",))
        (self.out / "libbase.dylib").write_bytes(macho_bytes(filetype=6))
        (problem,) = macho.outside_references(self.app)
        self.assertEqual((problem["file"], problem["dylib"]), ("Contents/MacOS/Example", "@rpath/libbase.dylib"))
        self.assertEqual(problem["resolves_to"], [os.path.realpath(self.out / "libbase.dylib")])

    def test_libraries_inside_the_bundle_and_system_libraries_are_allowed(self):
        self.executable("@rpath/Example Framework.framework/Example Framework")
        self.write(FRAMEWORK + "/Example Framework",
                   macho_bytes(["@loader_path/Libraries/libEGL.dylib", "/System/Library/Frameworks/AppKit.framework/AppKit",
                                "@executable_path/../Frameworks/Example Framework.framework/Example Framework"]))
        (self.app / "Contents/Frameworks/Example Framework.framework/Example Framework").symlink_to(
            "Versions/1.0/Example Framework")
        self.write(FRAMEWORK + "/Libraries/libEGL.dylib", macho_bytes(["@rpath/libvk.dylib"], ["@loader_path"]))
        self.write(FRAMEWORK + "/Libraries/libvk.dylib", macho_bytes())
        self.write("Contents/Resources/resources.pak", b"not a binary")
        self.assertEqual(macho.outside_references(self.app), [])

    def test_an_executable_run_path_is_relative_to_the_executable(self):
        self.executable(rpaths=("@loader_path/../Frameworks",))
        self.write(FRAMEWORK + "/Example Framework", macho_bytes(["@rpath/libshared.dylib"]))
        self.write("Contents/Frameworks/libshared.dylib", macho_bytes())
        self.assertEqual(macho.outside_references(self.app), [])

    def test_a_run_path_into_the_system_resolves_from_the_shared_cache(self):
        provided = {"/usr/lib/swift/libswiftCore.dylib", "/usr/lib/dyld"}
        patcher = mock.patch.object(macho, "_system_library", side_effect=lambda path: os.path.normpath(path) in provided)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.executable("@rpath/libswiftCore.dylib", rpaths=("/usr/lib/swift",))
        self.assertEqual(macho.outside_references(self.app), [])
        self.executable("@rpath/libbase.dylib", rpaths=("/usr/lib/swift", "@loader_path/../../.."))
        (self.out / "libbase.dylib").write_bytes(macho_bytes())
        self.assertEqual(len(macho.outside_references(self.app)), 1, "a system run path does not hide the output")
        self.executable("@rpath/libbase.dylib", rpaths=("/usr/lib/swift", "@loader_path/../Libraries"))
        self.assertEqual(len(macho.outside_references(self.app)), 1, "a library that exists nowhere is reported")
        self.executable("@rpath/dyld", rpaths=("/usr/lib",))
        self.assertEqual(macho.outside_references(self.app), [], "a system library that exists on disk is allowed")

    def test_a_system_library_is_one_on_disk_or_in_the_shared_cache(self):
        on_disk = self.write("on-disk.dylib", macho_bytes())
        cache = mock.Mock(side_effect=lambda encoded: encoded == b"/usr/lib/libcached.dylib")
        with mock.patch.object(macho, "_shared_cache_lookup", return_value=cache):
            self.assertTrue(macho._system_library(str(on_disk)))
            self.assertTrue(macho._system_library("/usr/lib/libcached.dylib"))
            self.assertFalse(macho._system_library("/usr/lib/libabsent.dylib"))
        with mock.patch.object(macho, "_shared_cache_lookup", return_value=None):
            self.assertFalse(macho._system_library("/usr/lib/libcached.dylib"), "no lookup means not provided")

    def test_without_a_readable_executable_executable_paths_start_in_contents_macos(self):
        self.write("Contents/MacOS/Example", b"not a binary")
        self.write("Contents/Frameworks/libone.dylib", macho_bytes(["@executable_path/../Frameworks/libtwo.dylib"]))
        self.write("Contents/Frameworks/libtwo.dylib", macho_bytes())
        self.assertEqual(macho.outside_references(self.app), [])

    def test_a_symbolic_link_out_of_the_bundle_counts_as_outside(self):
        self.executable("@rpath/libshared.dylib")
        (self.out / "libshared.dylib").write_bytes(macho_bytes())
        (self.app / "Contents/Frameworks").mkdir(parents=True)
        (self.app / "Contents/Frameworks/libshared.dylib").symlink_to(self.out / "libshared.dylib")
        self.assertEqual(len(macho.outside_references(self.app)), 1)

    def test_a_missing_required_library_is_reported_but_a_missing_weak_one_is_not(self):
        self.executable("@rpath/libmissing.dylib", ("@rpath/liboptional.dylib", 0x80000018))
        (problem,) = macho.outside_references(self.app)
        self.assertEqual((problem["dylib"], problem["resolves_to"]), ("@rpath/libmissing.dylib", []))

    def test_an_absolute_path_outside_the_system_is_reported(self):
        self.executable("/opt/homebrew/lib/libexample.dylib")
        self.assertEqual([item["dylib"] for item in macho.outside_references(self.app)],
                         ["/opt/homebrew/lib/libexample.dylib"])


class RecordedComponentBuildTests(BundleTestCase):
    def test_the_value_core_generated_is_read(self):
        self.out.mkdir(parents=True)
        self.assertIsNone(macos.recorded_component_build(self.out))
        (self.out / "args_generated.gni").write_text("is_debug=true\nis_component_build=true\n")
        self.assertIs(macos.recorded_component_build(self.out), True)
        (self.out / "args_generated.gni").write_text("is_component_build=false\n")
        self.assertIs(macos.recorded_component_build(self.out), False)


if __name__ == "__main__":
    unittest.main()
