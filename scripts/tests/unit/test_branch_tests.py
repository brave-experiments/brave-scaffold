# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Mapping modified test files to suites and filters, against disposable Git repositories."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from scaffold.brave import branch_tests
from scaffold.common.results import ScaffoldError

GIT = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
JAVA = "package org.example.app;\n\npublic class %s {}\n"
CPP = "TEST_F(%(f)s, One) {}\nTEST_F(%(f)s, Two) {}\nTEST(Other, Three) {}\n"
WEBUI_TEST = "suite('Alpha', function() {\n  test('a', () => {});\n});\nsuite('Beta', function() {\n  test('b', () => {});\n});\n"
WEBUI_CPP = "".join(
    "IN_PROC_BROWSER_TEST_F(WebUiTest, %s) {\n  RunTest(\"settings/x_test.js\", \"runMochaSuite('%s')\");\n}\n" % (n, n)
    for n in ("Alpha", "Beta"))


class BranchTestsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "src" / "brave"
        self.root.mkdir(parents=True)
        self.git("init", "-q", "-b", "master")
        self.write("README.md", "x\n")
        self.write("test/data/webui_tests_browsertest.cc", WEBUI_CPP)
        self.write("chromium_src/chrome/test/data/webui/settings/x_test.ts", WEBUI_TEST)
        self.commit("base")
        self.git("branch", "base-ref")

    def git(self, *args):
        subprocess.run([*GIT, "-C", str(self.root), *args], check=True, capture_output=True)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def found(self, scope="both"):
        return branch_tests.discover(self.root, "base-ref", scope)

    def phases(self, discovery):
        return {(p.target, p.suite): p.filters for p in discovery.phases}

    def test_java_cpp_and_android_native_files_map_to_suites_and_filters(self):
        self.write("android/javatests/org/example/app/FooTest.java", JAVA % "FooTest")
        self.write("android/junit/src/org/example/app/BarUnitTest.java", JAVA % "BarUnitTest")
        self.write("browser/foo_unittest.cc", CPP % {"f": "FooUnitTest"})
        self.write("browser/foo_browsertest.cc", "IN_PROC_BROWSER_TEST_F(FooBrowserTest, Runs) {}\n")
        self.write("browser/extensions/android/native_browsertest.cc", "IN_PROC_BROWSER_TEST_F(N, A) {}\n")
        self.write("browser/notes.cc", "int x;\n")
        self.commit("tests")
        discovery = self.found()
        self.assertEqual(self.phases(discovery), {
            ("android", "brave_junit_tests"): ["org.example.app.BarUnitTest.*"],
            ("android", "brave_java_unit_tests"): ["FooTest.*"],
            ("mac", "brave_unit_tests"): ["FooUnitTest.*", "Other.*"],
            ("mac", "brave_browser_tests"): ["FooBrowserTest.*"]})
        self.assertEqual([p.suite for p in discovery.phases],
                         ["brave_junit_tests", "brave_java_unit_tests", "brave_unit_tests", "brave_browser_tests"])
        self.assertEqual([path for path, _ in discovery.unmapped], ["browser/extensions/android/native_browsertest.cc"])
        self.assertEqual(len(discovery.test_files), 5)

    def test_parameterized_fixtures_match_their_instantiation_prefixes(self):
        self.write("browser/param_unittest.cc",
                   "TEST_P(ParamTest, Works) {}\nTEST_F(PlainTest, One) {}\nTEST_P(PlainTest2, Two) {}\n")
        self.write("browser/param_browsertest.cc",
                   "IN_PROC_BROWSER_TEST_P(ParamBrowserTest, Runs) {}\nIN_PROC_BROWSER_TEST_F(Plain, Runs) {}\n")
        self.commit("tests")
        self.assertEqual(self.phases(self.found()), {
            ("mac", "brave_unit_tests"): ["*/ParamTest.*", "*/PlainTest2.*", "PlainTest.*"],
            ("mac", "brave_browser_tests"): ["*/ParamBrowserTest.*", "Plain.*"]})

    def test_a_junit_file_without_a_readable_package_gets_a_wildcard_filter(self):
        self.write("android/junit/src/NoPackageTest.java", "public class NoPackageTest {}\n")
        self.commit("t")
        self.assertEqual(self.phases(self.found()), {("android", "brave_junit_tests"): ["*NoPackageTest.*"]})

    def test_pushing_the_branch_does_not_change_the_selection(self):
        self.write("browser/foo_unittest.cc", CPP % {"f": "FooUnitTest"})
        self.commit("tests")
        before = self.phases(self.found())
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "-u", "origin", "HEAD:refs/heads/topic")
        self.write("browser/bar_unittest.cc", CPP % {"f": "BarUnitTest"})
        self.commit("unpushed")
        after = self.phases(self.found())
        self.assertEqual(before, {("mac", "brave_unit_tests"): ["FooUnitTest.*", "Other.*"]})
        self.assertEqual(after, {("mac", "brave_unit_tests"): ["BarUnitTest.*", "FooUnitTest.*", "Other.*"]})

    def test_all_change_kinds_are_discovered_together(self):
        self.write("browser/committed_unittest.cc", CPP % {"f": "Committed"})
        self.commit("committed")
        self.write("browser/staged_unittest.cc", CPP % {"f": "Staged"})
        self.git("add", "browser/staged_unittest.cc")
        self.write("browser/committed_unittest.cc", CPP % {"f": "Committed"} + "// edit\n")
        self.write("browser/untracked_unittest.cc", CPP % {"f": "Untracked"})
        groups = {item["path"]: item["changes"] for item in self.found().to_dict()["test_files"]}
        self.assertEqual(groups, {"browser/committed_unittest.cc": ["committed", "unstaged"],
                                  "browser/staged_unittest.cc": ["staged"],
                                  "browser/untracked_unittest.cc": ["untracked"]})

    def test_named_files_run_whether_or_not_they_changed(self):
        self.write("chromium_src/chrome/test/data/webui/settings/x_test.ts", WEBUI_TEST + "\n")
        self.write("browser/old_unittest.cc", CPP % {"f": "Old"})
        self.commit("tests")
        self.git("branch", "-f", "base-ref", "HEAD")
        discovery = branch_tests.discover_files(self.root, ["browser/old_unittest.cc", "browser/notes.cc"])
        self.assertEqual(self.phases(discovery), {("mac", "brave_unit_tests"): ["Old.*", "Other.*"]})
        self.assertEqual(discovery.mode, "files")
        self.assertEqual(discovery.unmapped, [("browser/notes.cc", "not a recognized test file")])
        self.assertEqual(self.phases(self.found()), {}, "nothing changed against the base")

    def test_scope_separates_committed_from_working_tree_changes(self):
        self.write("a/committed_unittest.cc", CPP % {"f": "Committed"})
        self.commit("committed")
        self.write("a/staged_unittest.cc", CPP % {"f": "Staged"})
        self.git("add", "a/staged_unittest.cc")
        self.write("a/committed_unittest.cc", CPP % {"f": "Committed"} + "TEST(Edited, X) {}\n")
        self.write("a/untracked_unittest.cc", CPP % {"f": "Untracked"})
        both = self.phases(self.found("both"))[("mac", "brave_unit_tests")]
        self.assertEqual(both, ["Committed.*", "Edited.*", "Other.*", "Staged.*", "Untracked.*"])
        self.assertEqual(self.phases(self.found("committed"))[("mac", "brave_unit_tests")], ["Committed.*", "Other.*"])
        self.assertEqual(self.phases(self.found("worktree"))[("mac", "brave_unit_tests")],
                         ["Committed.*", "Edited.*", "Other.*", "Staged.*", "Untracked.*"])

    def test_deleted_tests_and_unmodified_tests_are_not_run(self):
        self.write("a/old_unittest.cc", CPP % {"f": "Old"})
        self.commit("old")
        self.git("branch", "-f", "base-ref")
        (self.root / "a" / "old_unittest.cc").unlink()
        self.commit("delete")
        self.assertEqual(self.found().phases, [])

    def test_a_webui_change_selects_only_the_changed_mocha_suite(self):
        self.write("chromium_src/chrome/test/data/webui/settings/x_test.ts",
                   WEBUI_TEST.replace("test('b', () => {});", "test('b', () => {}); // edited"))
        self.commit("edit beta")
        discovery = self.found()
        self.assertEqual(self.phases(discovery), {("mac", "brave_browser_tests"): ["WebUiTest.Beta"]})
        self.assertEqual(discovery.unmapped, [])

    def test_a_webui_test_without_a_registration_is_reported_not_guessed(self):
        self.write("chromium_src/chrome/test/data/webui/other/y_test.ts", WEBUI_TEST)
        self.commit("t")
        discovery = self.found()
        self.assertEqual(discovery.phases, [])
        self.assertIn("no C++ RunTest registration", discovery.unmapped[0][1])

    def test_an_unknown_base_is_an_input_error(self):
        with self.assertRaises(ScaffoldError) as caught:
            branch_tests.discover(self.root, "no-such-ref", "both")
        self.assertEqual(caught.exception.code, "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
