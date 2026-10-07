# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Run the test modules as parallel processes: `.venv/bin/python -m tests.run_parallel [--jobs N] [module ...]`.

Every module runs under `python -m unittest` in its own process. The suites build disposable sandboxes, so
modules do not share state; the largest modules are started first so the run ends when the slowest one does.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SUITES = ("unit", "integration")  # acceptance checks need a real checkout and are only run by name
MAX_DEFAULT_JOBS = 8
RAN = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)
SKIPPED = re.compile(r"skipped=(\d+)")
FAILURE_TAIL_LINES = 80


@dataclass
class Outcome:
    module: str
    seconds: float
    ran: int
    skipped: int
    returncode: int
    output: str

    @property
    def passed(self):
        return self.returncode == 0 and self.ran > 0


def default_jobs():
    return max(1, min(MAX_DEFAULT_JOBS, os.cpu_count() or 1))


def discover(root, suites=SUITES):
    """(module, test count) for every test file, largest first. The count only orders the work."""
    found = []
    for suite in suites:
        for path in sorted((Path(root) / "tests" / suite).glob("test_*.py")):
            count = len(re.findall(r"^\s+def test_", path.read_text(encoding="utf-8"), re.MULTILINE))
            found.append(("tests.%s.%s" % (suite, path.stem), count))
    return sorted(found, key=lambda item: (-item[1], item[0]))


def module_name(argument):
    """A dotted name for a test module given as a name or a path such as tests/unit/test_x.py."""
    if argument.endswith(".py") or "/" in argument:
        return Path(argument).with_suffix("").as_posix().strip("./").replace("/", ".")
    return argument


def run_module(root, module):
    started = time.perf_counter()
    environment = {**os.environ, "BCORE_NOTIFY_BACKEND": "none"}  # a test run never raises a desktop notification
    process = subprocess.run([sys.executable, "-m", "unittest", module], cwd=root, env=environment,
                             capture_output=True, text=True)
    output = process.stderr + process.stdout
    ran = RAN.search(output)
    skipped = SKIPPED.search(output.splitlines()[-1]) if output.strip() else None
    return Outcome(module, time.perf_counter() - started, int(ran.group(1)) if ran else 0,
                   int(skipped.group(1)) if skipped else 0, process.returncode, output)


def describe(outcome):
    return "%-4s %s (%d tests%s, %.1fs)" % (
        "ok" if outcome.passed else "FAIL", outcome.module, outcome.ran,
        ", %d skipped" % outcome.skipped if outcome.skipped else "", outcome.seconds)


def run(root, modules, jobs, out=None):
    """Run `modules` with up to `jobs` processes at once; the exit status is 0 only if every one passed."""
    out = out or sys.stdout
    started = time.perf_counter()
    outcomes = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = [pool.submit(run_module, root, module) for module in modules]
        for future in concurrent.futures.as_completed(futures):
            outcome = future.result()
            outcomes.append(outcome)
            print(describe(outcome), file=out, flush=True)
            if not outcome.passed:
                tail = outcome.output.strip().splitlines()[-FAILURE_TAIL_LINES:]
                print("\n".join("    " + line for line in tail) or "    (no output)", file=out, flush=True)
    except KeyboardInterrupt:
        pool.shutdown(wait=True, cancel_futures=True)
        print("interrupted", file=out, flush=True)
        return 130
    pool.shutdown()
    failed = [outcome for outcome in outcomes if not outcome.passed]
    total = sum(outcome.ran for outcome in outcomes)
    skipped = sum(outcome.skipped for outcome in outcomes)
    print("\nRan %d tests in %d modules with %d jobs in %.0fs%s." % (
        total, len(outcomes), jobs, time.perf_counter() - started,
        " (%d skipped)" % skipped if skipped else ""), file=out)
    if failed:
        print("FAILED: " + ", ".join(outcome.module for outcome in sorted(failed, key=lambda o: o.module)), file=out)
        return 1
    if total == 0:
        print("FAILED: no tests ran", file=out)
        return 1
    print("OK", file=out)
    return 0


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(prog="tests.run_parallel", description=__doc__.split("\n\n")[0])
    parser.add_argument("modules", nargs="*", help="test modules or files to run (default: the whole suite)")
    parser.add_argument("-j", "--jobs", type=int, default=default_jobs(),
                        help="modules to run at once (default: %%(default)s, the core count up to %d)" % MAX_DEFAULT_JOBS)
    parser.add_argument("--suite", choices=("unit", "integration", "all"), default="all",
                        help="which part of the suite to discover when no modules are named")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    if args.modules:
        modules = [module_name(item) for item in args.modules]
    else:
        modules = [name for name, _ in discover(root, SUITES if args.suite == "all" else (args.suite,))]
    if not modules:
        parser.error("no test modules found")
    return run(root, modules, min(args.jobs, len(modules)))


if __name__ == "__main__":
    sys.exit(main())
