# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The parallel test runner, against tiny disposable test projects."""

import contextlib
import io
import tempfile
import textwrap
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from tests import run_parallel

PASSING = """
import unittest
class T(unittest.TestCase):
    def test_one(self): pass
    def test_two(self): pass
"""
FAILING = """
import unittest
class T(unittest.TestCase):
    def test_ok(self): pass
    def test_breaks(self): self.assertEqual("expected-value", "actual-value")
"""
SKIPPING = """
import unittest
class T(unittest.TestCase):
    def test_runs(self): pass
    @unittest.skip("not here")
    def test_skipped(self): pass
"""
RENDEZVOUS = """
import os, time, unittest
MINE, THEIRS = %r, %r
class T(unittest.TestCase):
    def test_both_modules_run_at_the_same_time(self):
        open(MINE, "w").close()
        deadline = time.time() + 30
        while not os.path.exists(THEIRS) and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(THEIRS), "the other module never started")
"""


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for package in ("tests", "tests/unit", "tests/integration", "tests/acceptance"):
            (self.root / package).mkdir(parents=True, exist_ok=True)
            (self.root / package / "__init__.py").write_text("")

    def add(self, relative, source):
        (self.root / "tests" / relative).write_text(textwrap.dedent(source))

    def execute(self, modules, jobs=2):
        stream = io.StringIO()
        status = run_parallel.run(self.root, modules, jobs, out=stream)
        return status, stream.getvalue()

    def test_discovery_orders_the_largest_modules_first_and_skips_acceptance_and_helpers(self):
        self.add("unit/test_small.py", PASSING.replace("    def test_two(self): pass\n", ""))
        self.add("integration/test_big.py", PASSING + "    def test_three(self): pass\n")
        self.add("unit/helpers.py", PASSING)
        self.add("acceptance/test_real_checkout.py", PASSING)
        self.assertEqual(run_parallel.discover(self.root),
                         [("tests.integration.test_big", 3), ("tests.unit.test_small", 1)])
        self.assertEqual(run_parallel.discover(self.root, ("unit",)), [("tests.unit.test_small", 1)])

    def test_files_and_dotted_names_both_name_a_module(self):
        for given in ("tests/unit/test_x.py", "./tests/unit/test_x.py", "tests.unit.test_x"):
            self.assertEqual(run_parallel.module_name(given), "tests.unit.test_x", given)
        self.assertEqual(run_parallel.module_name("tests.unit.test_x.T.test_one"), "tests.unit.test_x.T.test_one")

    def test_passing_modules_succeed_and_are_counted(self):
        self.add("unit/test_a.py", PASSING)
        self.add("integration/test_b.py", PASSING)
        status, output = self.execute(["tests.unit.test_a", "tests.integration.test_b"])
        self.assertEqual(status, 0, output)
        self.assertIn("Ran 4 tests in 2 modules with 2 jobs", output)
        self.assertRegex(output, r"ok\s+tests\.unit\.test_a \(2 tests, ")
        self.assertTrue(output.rstrip().endswith("OK"))

    def test_a_failing_module_fails_the_run_shows_why_and_does_not_stop_the_others(self):
        self.add("unit/test_good.py", PASSING)
        self.add("unit/test_bad.py", FAILING)
        status, output = self.execute(["tests.unit.test_bad", "tests.unit.test_good"])
        self.assertEqual(status, 1)
        self.assertRegex(output, r"FAIL tests\.unit\.test_bad \(2 tests")
        self.assertIn("actual-value", output, "the failing test's message is shown")
        self.assertRegex(output, r"ok\s+tests\.unit\.test_good")
        self.assertIn("FAILED: tests.unit.test_bad", output)
        self.assertNotIn("\nOK", output)

    def test_skipped_tests_are_reported_and_do_not_fail_the_run(self):
        self.add("unit/test_skips.py", SKIPPING)
        status, output = self.execute(["tests.unit.test_skips"])
        self.assertEqual(status, 0, output)
        self.assertIn("(2 tests, 1 skipped,", output)
        self.assertIn("(1 skipped)", output)

    def test_a_module_that_cannot_be_imported_or_runs_no_tests_is_a_failure(self):
        self.add("unit/test_broken.py", "this is not python\n")
        self.add("unit/test_empty.py", "import unittest\n")
        status, output = self.execute(["tests.unit.test_broken", "tests.unit.test_empty"])
        self.assertEqual(status, 1)
        self.assertIn("FAILED: tests.unit.test_broken, tests.unit.test_empty", output)

    def test_modules_really_run_at_the_same_time(self):
        a, b = self.root / "a.flag", self.root / "b.flag"
        self.add("unit/test_first.py", RENDEZVOUS % (str(a), str(b)))
        self.add("unit/test_second.py", RENDEZVOUS % (str(b), str(a)))
        status, output = self.execute(["tests.unit.test_first", "tests.unit.test_second"], jobs=2)
        self.assertEqual(status, 0, output)

    def test_named_modules_and_options_through_the_command_line(self):
        self.add("unit/test_a.py", PASSING)
        self.add("unit/test_b.py", FAILING)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = run_parallel.main(["--jobs", "1", "tests/unit/test_a.py"], root=self.root)
        self.assertEqual(status, 0, stream.getvalue())
        self.assertIn("Ran 2 tests in 1 modules with 1 jobs", stream.getvalue())
        self.assertNotIn("test_b", stream.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            run_parallel.main(["--jobs", "0"], root=self.root)
        self.assertEqual(caught.exception.code, 2)

    def test_the_default_job_count_is_bounded(self):
        self.assertGreaterEqual(run_parallel.default_jobs(), 1)
        self.assertLessEqual(run_parallel.default_jobs(), run_parallel.MAX_DEFAULT_JOBS)

    def test_child_processes_never_raise_desktop_notifications(self):
        self.add("unit/test_env.py", """
            import os, unittest
            class T(unittest.TestCase):
                def test_backend(self): self.assertEqual(os.environ.get("BCORE_NOTIFY_BACKEND"), "none")
            """)
        status, output = self.execute(["tests.unit.test_env"])
        self.assertEqual(status, 0, output)


if __name__ == "__main__":
    unittest.main()
