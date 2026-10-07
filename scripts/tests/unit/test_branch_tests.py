# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Mapping modified test files to suites and filters, against disposable Git repositories."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from scaffold.brave import branch_tests
from scaffold.common.procs import ProcessResult
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

    def test_typed_fixtures_get_filters_that_match_their_type_indexed_names(self):
        self.write("browser/typed_unittest.cc",
                   "TEST_F(PlainTest, One) {}\nTYPED_TEST(TypedTest, Works) {}\nTYPED_TEST_P(TypedParamTest, Works) {}\n"
                   "TEST_P(ValueParamTest, Works) {}\n")
        self.write("browser/typed_browsertest.cc", "IN_PROC_BROWSER_TEST_F(PlainBrowserTest, A) {}\n"
                   "  TYPED_TEST(TypedBrowserTest, B) {}\n")
        self.commit("typed tests")
        discovery = self.found()
        self.assertEqual(self.phases(discovery), {
            ("mac", "brave_unit_tests"): ["*/TypedParamTest/*.*", "*/ValueParamTest.*", "PlainTest.*", "TypedTest/*.*"],
            ("mac", "brave_browser_tests"): ["PlainBrowserTest.*", "TypedBrowserTest/*.*"]})
        self.assertEqual(discovery.unmapped, [])

    def test_a_file_with_only_typed_fixtures_is_mapped_too(self):
        self.write("browser/only_typed_unittest.cc", "TYPED_TEST(OnlyTyped, Works) {}\n")
        self.commit("only typed")
        self.assertEqual(self.phases(self.found()), {("mac", "brave_unit_tests"): ["OnlyTyped/*.*"]})

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

    def test_unusual_file_names_are_discovered_in_every_change_kind(self):
        self.write("browser/café_unittest.cc", CPP % {"f": "Committed"})
        self.commit("committed")
        self.write("browser/with space_unittest.cc", CPP % {"f": "Staged"})
        self.git("add", "browser/with space_unittest.cc")
        self.write("browser/café_unittest.cc", CPP % {"f": "Committed"} + "// edit\n")
        self.write("browser/ünïcode_unittest.cc", CPP % {"f": "Untracked"})
        groups = {item["path"]: item["changes"] for item in self.found().to_dict()["test_files"]}
        self.assertEqual(groups, {"browser/café_unittest.cc": ["committed", "unstaged"],
                                  "browser/with space_unittest.cc": ["staged"],
                                  "browser/ünïcode_unittest.cc": ["untracked"]})
        self.assertEqual(self.found().unmapped, [])

    def test_a_webui_harness_in_a_file_with_spaces_is_found(self):
        self.git("mv", "test/data/webui_tests_browsertest.cc", "test/data/webui tests_browsertest.cc")
        self.commit("rename harness")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write("chromium_src/chrome/test/data/webui/settings/x_test.ts",
                   WEBUI_TEST.replace("test('b', () => {});", "test('b', () => {}); // edited"))
        self.commit("edit beta")
        discovery = self.found()
        self.assertEqual(self.phases(discovery), {("mac", "brave_browser_tests"): ["WebUiTest.Beta"]})
        self.assertEqual(discovery.unmapped, [])

    def test_a_committed_file_too_large_to_read_completely_stops_discovery(self):
        filler = "// filler line to make the file large enough to exceed one megabyte\n" * 16000
        self.write("browser/big_unittest.cc", "TEST_F(EarlyTest, A) {}\n" + filler + "TEST_F(LateTest, B) {}\n")
        self.commit("big test")
        self.assertGreater(len((self.root / "browser/big_unittest.cc").read_bytes()), 1_000_000)
        with self.assertRaises(ScaffoldError) as caught:
            self.found("committed")
        self.assertEqual(caught.exception.code, "READINESS_INCOMPLETE")
        self.assertIn("too large", caught.exception.message)
        self.assertEqual(self.phases(self.found("both")), {("mac", "brave_unit_tests"): ["EarlyTest.*", "LateTest.*"]},
                         "the working-tree file is read whole")

    def test_truncated_or_timed_out_git_output_never_becomes_a_partial_selection(self):
        cut = ProcessResult(returncode=0, stdout="a_browsertest.cc\0", truncated=True)
        slow = ProcessResult(returncode=124, stdout="", timed_out=True)
        repo = branch_tests.Repo(self.root)
        for label, result in (("truncated", cut), ("timed out", slow)):
            for check in (True, False):
                with self.subTest(label=label, check=check), \
                        mock.patch.object(branch_tests, "run_capture", return_value=result):
                    with self.assertRaises(ScaffoldError) as caught:
                        repo.git("show", "HEAD:x", check=check)
                    self.assertEqual(caught.exception.code, "READINESS_INCOMPLETE")
            with self.subTest(label=label, call="webui harness search"), \
                    mock.patch.object(branch_tests, "run_capture", return_value=result):
                with self.assertRaises(ScaffoldError) as caught:
                    branch_tests.find_webui_harnesses(repo, "settings/x_test.js")
                self.assertEqual(caught.exception.code, "READINESS_INCOMPLETE")

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

    SHARED_WEBUI = ("const shared = 1;\nsuite('Alpha', function() {\n  test('a', () => shared);\n});\n"
                    "suite('Beta', function() {\n  test('b', () => shared);\n});\n")

    def webui_phase(self, discovery):
        return self.phases(discovery).get(("mac", "brave_browser_tests"))

    def test_naming_a_webui_file_runs_every_suite_registered_for_it(self):
        discovery = branch_tests.discover_files(self.root, ["chromium_src/chrome/test/data/webui/settings/x_test.ts"])
        self.assertEqual(self.webui_phase(discovery), ["WebUiTest.Alpha", "WebUiTest.Beta"])
        self.assertEqual(discovery.unmapped, [])

    def test_a_change_outside_every_suite_selects_all_of_them(self):
        webui = "chromium_src/chrome/test/data/webui/settings/x_test.ts"
        self.write(webui, self.SHARED_WEBUI)
        self.commit("shared value")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write(webui, self.SHARED_WEBUI.replace("shared = 1", "shared = 2").replace("() => shared);\n});\nsuite",
                                                    "() => shared + 0);\n});\nsuite"))
        self.commit("change the shared value and one suite")
        discovery = self.found()
        self.assertEqual(self.webui_phase(discovery), ["WebUiTest.Alpha", "WebUiTest.Beta"])
        self.assertEqual(discovery.unmapped, [])

    def test_a_change_only_outside_the_suites_is_not_left_unmapped(self):
        webui = "chromium_src/chrome/test/data/webui/settings/x_test.ts"
        self.write(webui, self.SHARED_WEBUI)
        self.commit("shared value")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write(webui, self.SHARED_WEBUI.replace("shared = 1", "shared = 2"))
        self.commit("change only the shared value")
        discovery = self.found()
        self.assertEqual(self.webui_phase(discovery), ["WebUiTest.Alpha", "WebUiTest.Beta"])
        self.assertEqual(discovery.unmapped, [])

    def test_a_change_inside_one_suite_still_selects_only_that_suite(self):
        webui = "chromium_src/chrome/test/data/webui/settings/x_test.ts"
        self.write(webui, self.SHARED_WEBUI)
        self.commit("shared value")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write(webui, self.SHARED_WEBUI.replace("test('b', () => shared);", "test('b', () => shared + 1);"))
        self.commit("edit beta only")
        self.assertEqual(self.webui_phase(self.found()), ["WebUiTest.Beta"])

    SETUP_BETWEEN = ("suite('Alpha', function() {\n  test('a', () => {});\n});\nconst shared = 1;\n"
                     "suite('Beta', function() {\n  test('b', () => {});\n});\n")
    BETA_WITH_TWO_TESTS = ("suite('Alpha', function() {\n  test('a', () => {});\n});\n"
                           "suite('Beta', function() {\n  test('b', () => {});\n  test('c', () => {});\n});\n")

    def test_deleting_shared_setup_between_suites_selects_every_suite(self):
        webui = "chromium_src/chrome/test/data/webui/settings/x_test.ts"
        self.write(webui, self.SETUP_BETWEEN)
        self.commit("setup between the suites")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write(webui, self.SETUP_BETWEEN.replace("const shared = 1;\n", ""))
        self.commit("delete the shared setup")
        discovery = self.found()
        self.assertEqual(self.webui_phase(discovery), ["WebUiTest.Alpha", "WebUiTest.Beta"])
        self.assertEqual(discovery.unmapped, [])

    def test_deleting_a_line_inside_one_suite_still_selects_only_that_suite(self):
        webui = "chromium_src/chrome/test/data/webui/settings/x_test.ts"
        self.write(webui, self.BETA_WITH_TWO_TESTS)
        self.commit("two beta tests")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write(webui, self.BETA_WITH_TWO_TESTS.replace("  test('c', () => {});\n", ""))
        self.commit("delete one beta test")
        self.assertEqual(self.webui_phase(self.found()), ["WebUiTest.Beta"])

    def test_removed_lines_are_placed_by_the_suites_of_the_old_file(self):
        old = self.SETUP_BETWEEN  # Alpha is lines 1-3, shared setup line 4, Beta lines 5-7
        new = old.replace("const shared = 1;\n", "")  # Alpha is lines 1-3, Beta lines 4-6
        select = branch_tests.mocha_selection
        self.assertEqual(select(new, set(), old, {2}), {"Alpha"})
        self.assertEqual(select(new, set(), old, {6}), {"Beta"})
        self.assertIs(select(new, set(), old, {4}), branch_tests.ALL_SUITES, "removed from between the suites")
        self.assertEqual(select(new, {4}, old, {5}), {"Beta"}, "a suite's own declaration line was reworded")
        self.assertIs(select(new, {4}, old, {4, 5}), branch_tests.ALL_SUITES,
                      "shared setup removed together with the declaration line after it")
        self.assertIs(select(new, {2}, old, {4}), branch_tests.ALL_SUITES, "an inside edit does not hide a shared one")
        self.assertIs(select(new, set(), "", {1}), branch_tests.ALL_SUITES, "no old file to place removed lines in")

    def test_removed_lines_of_a_suite_that_no_longer_exists_select_nothing_extra(self):
        old = self.SETUP_BETWEEN
        only_alpha = "suite('Alpha', function() {\n  test('a', () => {});\n});\n"
        self.assertEqual(branch_tests.mocha_selection(only_alpha, set(), old, {6}), set())

    def test_deleting_shared_setup_while_editing_the_adjacent_suite_selects_every_suite(self):
        webui = "chromium_src/chrome/test/data/webui/settings/x_test.ts"
        self.write(webui, self.SETUP_BETWEEN)
        self.commit("setup between the suites")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write(webui, self.SETUP_BETWEEN.replace("const shared = 1;\nsuite('Beta', function() {",
                                                    "suite('Beta', function() { // reworked"))
        self.commit("delete the setup and reword the next line, which git reports as one hunk")
        discovery = self.found()
        self.assertEqual(self.webui_phase(discovery), ["WebUiTest.Alpha", "WebUiTest.Beta"])
        self.assertEqual(discovery.unmapped, [])

    def test_rewording_only_a_suite_declaration_still_selects_only_that_suite(self):
        webui = "chromium_src/chrome/test/data/webui/settings/x_test.ts"
        self.write(webui, self.SETUP_BETWEEN)
        self.commit("setup between the suites")
        self.git("branch", "-f", "base-ref", "HEAD")
        self.write(webui, self.SETUP_BETWEEN.replace("suite('Beta', function() {", "suite('Beta', function() { // reworked"))
        self.commit("reword Beta's declaration")
        self.assertEqual(self.webui_phase(self.found()), ["WebUiTest.Beta"])

    def test_the_selection_helper_distinguishes_suite_lines_from_shared_lines(self):
        content = self.SHARED_WEBUI
        self.assertEqual(branch_tests.mocha_selection(content, {3}), {"Alpha"})
        self.assertEqual(branch_tests.mocha_selection(content, {3, 6}), {"Alpha", "Beta"})
        self.assertIs(branch_tests.mocha_selection(content, {1}), branch_tests.ALL_SUITES)
        self.assertIs(branch_tests.mocha_selection(content, {3, 1}), branch_tests.ALL_SUITES)
        self.assertEqual(branch_tests.mocha_selection(content, set()), set())

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
