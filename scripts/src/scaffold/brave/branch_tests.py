# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Find the test files a branch or working tree modifies and map them to suites and filters (read-only)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..common.procs import run_capture
from ..common.results import ScaffoldError, repair

SCOPES = ("both", "committed", "worktree")
DEFAULT_BASE = "origin/master"
CPP_TEST_CASE_RE = re.compile(
    r"^\s*(?P<macro>IN_PROC_BROWSER_TEST(?:_[A-Z]+)?|TEST(?:_[A-Z]+)?|TEST_F|TEST_P)\s*"
    r"\(\s*(?P<fixture>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*(?P<test>[A-Za-z_][A-Za-z0-9_]*)\s*\)", re.MULTILINE)
JAVA_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", re.MULTILINE)
DIFF_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(?P<start>\d+)(?:,(?P<count>\d+))?\s+@@", re.MULTILINE)
NAMED_FUNCTION_RE = re.compile(r"\b(?:async\s+)?function\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\([^)]*\)\s*\{",
                               re.MULTILINE)
NAMED_SUITE_RE = re.compile(r"\bsuite\s*\(\s*(['\"])(?P<suite>[^'\"]+)\1\s*,\s*(?P<function>[A-Za-z_$][A-Za-z0-9_$]*)\b",
                            re.MULTILINE)
INLINE_SUITE_RE = re.compile(
    r"\bsuite\s*\(\s*(['\"])(?P<suite>[^'\"]+)\1\s*,\s*"
    r"(?:(?:async\s+)?function(?:\s+[A-Za-z_$][A-Za-z0-9_$]*)?\s*\([^)]*\)"
    r"|(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>)\s*\{", re.MULTILINE)
MOCHA_SUITE_RE = re.compile(r"\brunMochaSuite\s*\(\s*(['\"])(?P<suite>[^'\"]+)\1\s*\)")
WEBUI_TEST_PREFIXES = ("chromium_src/chrome/test/data/webui/", "chrome/test/data/webui/")
# Run order: quick host suites first.
SUITE_ORDER = (("android", "brave_junit_tests"), ("android", "brave_java_unit_tests"),
               ("mac", "brave_unit_tests"), ("mac", "brave_browser_tests"))


@dataclass(frozen=True)
class WebUIHarness:
    test_filter: str
    mocha_suite: str | None
    source: str


@dataclass
class Phase:
    target: str
    suite: str
    filters: list = field(default_factory=list)
    files: list = field(default_factory=list)

    @property
    def filter(self):
        return ":".join(self.filters)

    def to_dict(self):
        return {"target": self.target, "suite": self.suite, "filter": self.filter, "filters": self.filters,
                "files": self.files}


@dataclass
class Discovery:
    base: str
    scope: str
    considered: int
    test_files: list
    phases: list
    unmapped: list
    mode: str = "changed"
    deselected: list = field(default_factory=list)

    def to_dict(self):
        return {"mode": self.mode, "base": self.base, "scope": self.scope, "changed_files": self.considered,
                "test_files": self.test_files, "phases": [phase.to_dict() for phase in self.phases],
                "unmapped": [{"path": path, "reason": reason} for path, reason in self.unmapped],
                "deselected": [{"path": path, "reason": reason} for path, reason in self.deselected]}


def require_complete(result, what):
    """A truncated or timed-out Git answer must stop discovery: a partial selection would look like a complete one."""
    if result.truncated or result.timed_out:
        raise ScaffoldError(
            "READINESS_INCOMPLETE", "%s %s, so test selection stopped instead of reporting a partial one." % (
                what, "timed out" if result.timed_out else "was too large to read completely"),
            details={"what": what})


class Repo:
    def __init__(self, path, log=None):
        self.path, self.log = Path(path), log

    def git(self, *args, check=True):
        result = run_capture(["git", "-C", str(self.path), *args], str(self.path), None, self.log, timeout=120)
        require_complete(result, "git %s output" % " ".join(args[:2]))
        if check and result.returncode != 0:
            raise ScaffoldError("CHILD_FAILED", "git %s failed: %s" % (" ".join(args[:2]), result.stderr.strip()[-300:]),
                                details={"argv": ["git", *args]}, child_exit_code=result.returncode)
        return result.stdout

    def names(self, *args):
        """Paths from a Git command run with -z: NUL-separated, never quoted or reflowed."""
        return [name for name in self.git(*args).split("\0") if name]

    def read(self, path):
        full = self.path / path
        return full.read_text(encoding="utf-8", errors="replace") if full.is_file() else ""


def require_base(repo, base):
    if run_capture(["git", "-C", str(repo.path), "rev-parse", "--verify", "-q", base + "^{commit}"], str(repo.path),
                   None, repo.log, timeout=30).returncode != 0:
        raise ScaffoldError("INVALID_INPUT", "The base ref %r does not exist in this checkout." % base,
                            details={"example": "bcore test --base origin/master"},
                            repairs=[repair(["git", "-C", str(repo.path), "branch", "-a"], note="Lists refs.")])


def changed_files(repo, base, scope):
    groups = {"committed": set(), "staged": set(), "unstaged": set(), "untracked": set()}
    if scope in ("both", "committed"):
        groups["committed"].update(repo.names("diff", "-z", "--name-only", "--diff-filter=d", base + "...HEAD"))
    if scope in ("both", "worktree"):
        groups["staged"].update(repo.names("diff", "-z", "--cached", "--name-only", "--diff-filter=d"))
        groups["unstaged"].update(repo.names("diff", "-z", "--name-only", "--diff-filter=d"))
        groups["untracked"].update(repo.names("ls-files", "-z", "--others", "--exclude-standard"))
    return groups


def _wrapped(path):
    return "/" + path.replace("\\", "/")


def is_java_test(path):
    wrapped = _wrapped(path)
    return wrapped.endswith(".java") and ("/javatests/" in wrapped or "/junit/" in wrapped
                                          or Path(path).name.endswith("Test.java"))


def android_java_suite(path):
    wrapped = _wrapped(path)
    return "brave_junit_tests" if "/junit/" in wrapped else "brave_java_unit_tests" if "/javatests/" in wrapped else None


def is_cpp_browser_test(path):
    return Path(path).name.endswith(("_browsertest.cc", "_uitest.cc"))


def is_cpp_unit_test(path):
    return Path(path).name.endswith("_unittest.cc")


def webui_test_target(path):
    normalized = path.replace("\\", "/")
    for prefix in WEBUI_TEST_PREFIXES:
        if normalized.startswith(prefix) and normalized.endswith((".ts", ".js")):
            return normalized[len(prefix):].rsplit(".", 1)[0] + ".js"
    return None


def is_test_file(path):
    return is_java_test(path) or is_cpp_browser_test(path) or is_cpp_unit_test(path) or webui_test_target(path) is not None


def matching_brace(text, opening):
    depth, state, index = 0, "code", opening
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and following and following in "/*":
                state = "line-comment" if following == "/" else "block-comment"
                index += 2
                continue
            if char in "'\"`":
                state = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
        elif state == "line-comment":
            if char == "\n":
                state = "code"
        elif state == "block-comment":
            if char == "*" and following == "/":
                state = "code"
                index += 2
                continue
        elif char == "\\":
            index += 2
            continue
        elif char == state:
            state = "code"
        index += 1
    return None


def _line(text, offset):
    return text.count("\n", 0, offset) + 1


def mocha_suite_spans(content):
    functions = {}
    for match in NAMED_FUNCTION_RE.finditer(content):
        closing = matching_brace(content, match.end() - 1)
        if closing is not None:
            functions[match.group("name")] = (_line(content, match.start()), _line(content, closing))
    spans = set()
    for match in NAMED_SUITE_RE.finditer(content):
        span = functions.get(match.group("function"))
        if span:
            spans.add((match.group("suite"), *span))
        line = _line(content, match.start())
        spans.add((match.group("suite"), line, line))
    for match in INLINE_SUITE_RE.finditer(content):
        closing = matching_brace(content, match.end() - 1)
        if closing is not None:
            spans.add((match.group("suite"), _line(content, match.start()), _line(content, closing)))
    return sorted(spans)


ALL_SUITES = None  # every suite registered for the file


def changed_mocha_suites(content, lines):
    return {suite for suite, start, end in mocha_suite_spans(content) if any(start <= n <= end for n in lines)}


def mocha_selection(content, lines):
    """The suites the changed lines touch, or ALL_SUITES when a change lies outside every suite body.

    A line outside the suites may be a helper, fixture, or value any of them uses, so it cannot be tied to one.
    """
    spans = mocha_suite_spans(content)
    if any(not any(start <= number <= end for _, start, end in spans) for number in lines):
        return ALL_SUITES
    return changed_mocha_suites(content, lines)


def changed_lines(repo, path, base, scope, groups, content):
    if path in groups["untracked"]:
        return set(range(1, len(content.splitlines()) + 1))
    if scope == "committed":
        args = ["diff", "--unified=0", base + "...HEAD", "--", path]
    elif scope == "worktree":
        args = ["diff", "--unified=0", "HEAD", "--", path]
    else:
        args = ["diff", "--unified=0", repo.git("merge-base", base, "HEAD").strip(), "--", path]
    found = set()
    for match in DIFF_HUNK_RE.finditer(repo.git(*args)):
        start, count = int(match.group("start")), int(match.group("count") or "1")
        found.update({start} if count == 0 else range(start, start + count))
    return found


def parse_webui_harnesses(content, target, source):
    cases = list(CPP_TEST_CASE_RE.finditer(content))
    target_re = re.compile(r"\bRunTest\s*\(\s*\"" + re.escape(target) + r"\"")
    found = []
    for index, match in enumerate(cases):
        limit = cases[index + 1].start() if index + 1 < len(cases) else len(content)
        opening = content.find("{", match.end(), limit)
        closing = matching_brace(content, opening) if opening != -1 else None
        if closing is None:
            continue
        body = content[opening:closing + 1]
        hit = target_re.search(body)
        if not hit:
            continue
        end = body.find(";", hit.end())
        suite = MOCHA_SUITE_RE.search(body[hit.start():end if end != -1 else len(body)])
        fixture, test = match.group("fixture"), match.group("test")
        if match.group("macro").endswith("_P"):
            selected = "*%s.%s*" % (fixture, test)
        elif test.startswith("MAYBE_"):
            selected = fixture + ".*"
        else:
            selected = "%s.%s" % (fixture, test)
        found.append(WebUIHarness(selected, suite.group("suite") if suite else None, source))
    return found


def find_webui_harnesses(repo, target):
    roots = [repo]
    if (repo.path.parent / ".git").exists():
        roots.append(Repo(repo.path.parent, repo.log))
    found = set()
    for root in roots:
        result = run_capture(["git", "-C", str(root.path), "grep", "-lz", "-F", target, "--", "*_browsertest.cc",
                              "*_uitest.cc"], str(root.path), None, root.log, timeout=120)
        require_complete(result, "git grep output in %s" % root.path)
        if result.returncode not in (0, 1):
            raise ScaffoldError("CHILD_FAILED", "git grep failed in %s" % root.path, child_exit_code=result.returncode)
        for name in filter(None, result.stdout.split("\0")):
            found.update(parse_webui_harnesses(root.read(name), target, "%s:%s" % (root.path, name)))
    return sorted(found, key=lambda item: (item.test_filter, item.source))


def select_webui_harnesses(harnesses, suites):
    if not harnesses:
        return [], "no C++ RunTest registration found"
    if suites is ALL_SUITES:
        return list(harnesses), None
    unnamed = [item for item in harnesses if item.mocha_suite is None]
    if suites:
        explicit = [item for item in harnesses if item.mocha_suite in suites]
        if {item.mocha_suite for item in explicit} == suites:
            return explicit, None
        if len(unnamed) == 1:
            return unnamed, None
        missing = ", ".join(sorted(suites - {item.mocha_suite for item in explicit}))
        return [], "no unambiguous C++ harness selects changed Mocha suite(s): %s" % missing
    if len(harnesses) == 1:
        return harnesses, None
    if len(unnamed) == 1:
        return unnamed, None
    return [], "multiple C++ RunTest registrations matched: %s" % ", ".join(item.test_filter for item in harnesses)


def content_of(repo, path, scope):
    """The file as the inspected scope sees it: HEAD for committed changes, else the working tree."""
    return repo.git("show", "HEAD:" + path, check=False) if scope == "committed" else repo.read(path)


def java_filter(content, path, suite):
    name = Path(path).stem
    if suite != "brave_junit_tests":
        return name + ".*"
    package = JAVA_PACKAGE_RE.search(content)
    return "%s.%s.*" % (package.group(1), name) if package else "*%s.*" % name


def map_file(repo, path, base, scope, groups, phases, unmapped):
    def add(target, suite, filters):
        phase = phases.setdefault((target, suite), Phase(target, suite))
        phase.filters = sorted({*phase.filters, *filters})
        if path not in phase.files:
            phase.files.append(path)

    if is_java_test(path):
        suite = android_java_suite(path)
        if suite is None:
            unmapped.append((path, "Java test is outside the supported javatests and junit layouts"))
        else:
            add("android", suite, [java_filter(content_of(repo, path, scope), path, suite)])
        return
    if is_cpp_browser_test(path) or is_cpp_unit_test(path):
        wrapped = _wrapped(path)
        for part, name in (("/android/", "Android"), ("/ios/", "iOS")):
            if part in wrapped:
                unmapped.append((path, "%s native tests are not available through bcore" % name))
                return
        filters = {("*/%s.*" if m.group("macro").endswith("_P") else "%s.*") % m.group("fixture")
                   for m in CPP_TEST_CASE_RE.finditer(content_of(repo, path, scope))}
        if not filters:
            unmapped.append((path, "no C++ test fixture macro found in the file"))
        else:
            add("mac", "brave_browser_tests" if is_cpp_browser_test(path) else "brave_unit_tests", sorted(filters))
        return
    target = webui_test_target(path)
    if target:
        content = content_of(repo, path, scope)
        if not content:
            unmapped.append((path, "test file is missing or empty"))
            return
        suites = (mocha_selection(content, changed_lines(repo, path, base, scope, groups, content))
                  if base else ALL_SUITES)
        selected, reason = select_webui_harnesses(find_webui_harnesses(repo, target), suites)
        if reason:
            unmapped.append((path, "%s for %s" % (reason, target)))
        else:
            add("mac", "brave_browser_tests", [item.test_filter for item in selected])
        return
    unmapped.append((path, "test type is not supported"))


def discover_files(core, paths, log=None):
    """Map the named files, changed or not; `paths` are checkout-relative and already verified to exist."""
    repo = Repo(core, log)
    phases, unmapped = {}, []
    tests = []
    for path in paths:
        if is_test_file(path):
            tests.append(path)
            map_file(repo, path, None, "worktree", {}, phases, unmapped)
        else:
            unmapped.append((path, "not a recognized test file"))
    ordered = [phases[key] for key in SUITE_ORDER if key in phases]
    return Discovery(None, "files", len(paths), [{"path": path, "changes": []} for path in tests], ordered, unmapped,
                     mode="files")


def discover(core, base=DEFAULT_BASE, scope="both", log=None):
    repo = Repo(core, log)
    require_base(repo, base)
    groups = changed_files(repo, base, scope)
    paths = sorted(set().union(*groups.values()))
    tests = [path for path in paths if is_test_file(path)]
    phases, unmapped = {}, []
    for path in tests:
        map_file(repo, path, base, scope, groups, phases, unmapped)
    ordered = [phases[key] for key in SUITE_ORDER if key in phases]
    labelled = [{"path": path, "changes": [name for name, found in groups.items() if path in found]} for path in tests]
    return Discovery(base, scope, len(paths), labelled, ordered, unmapped)
