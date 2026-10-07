# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Documented command examples run in fixtures and behave as the guides say; none is left unclassified."""

import json
import re
import shlex
import unittest
from pathlib import Path

from tests.integration.test_android import AndroidTestCase
from tests.integration.test_build import SKIP, BuildTestCase
from tests.support import SandboxTest

ROOT = Path(__file__).resolve().parents[3]
FENCE = re.compile(r"```(?:sh|bash|shell)\n(.*?)```", re.S)
COMMAND = re.compile(r"^(?:scripts/)?(bcore|bpm)\b")

# Examples that show syntax or need something a disposable fixture cannot provide.
NOT_RUN = {
    "bpm [--checkout <name-or-path>] [--config <file>] [--json] <package arguments...>": "syntax reference",
    "bcore vpython3 [--checkout <name-or-path>] [--cwd <directory>] [--] <arguments...>": "syntax reference",
    "bcore android setup": "clones the default support repository from the network",
    "bcore sync ios": "needs Core's iOS bootstrap hook; covered by tests/integration/test_ios.py",
    "bcore sync ios,android": "needs Core's iOS bootstrap hook; covered by tests/integration/test_ios.py",
    'bcore build ios': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    'bcore build ios --device "iPhone 16"': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    'bcore build ios --plan': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    'bcore build ios -jobs 4 CODE_SIGNING_ALLOWED=NO': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    'bcore build-run ios': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    'bcore run ios': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    'bcore run ios --device "iPhone 16 Pro"': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    'bcore sync-build ios': 'needs an iOS checkout and Xcode; covered by tests/integration/test_ios.py',
    "bcore android setup --ref <tag-or-sha>": "clones the default support repository from the network",
    'bcore test mac --plan': "needs a Core branch with a base ref; covered by tests.integration.test_test_changed",
    'bcore test mac': "needs modified Core test files; covered by tests.integration.test_test_changed",
    'bcore test android': "needs modified Core test files and Android support; covered by tests.integration.test_test_changed",
    'bcore test android --plan': "needs a Core branch with a base ref; covered by tests.integration.test_test_changed",
    'bcore test android --device=emulator-5554': "needs a Core branch with a base ref; covered by tests.integration.test_test_changed",
    'bcore test --file components/example/example_unittest.cc': "needs a Core branch with a base ref; covered by tests.integration.test_test_changed",
    "bcore test android brave_junit_tests --filter='*BraveCommandLineInitUtilTest*'":
        "needs the Android test support fixtures; run by tests.integration.test_android_tests",
    "bcore test android brave_java_unit_tests --filter='BraveAppearancePreferencesTest.*' --device=emulator-5554":
        "needs the Android test support fixtures; run by tests.integration.test_android_tests",
}

# Fixture state an example assumes, and the exit codes that show it behaved as documented.
BUILT, BUILT_CUSTOM, ANDROID, ANDROID_BUILT = "built", "built-custom", "android", "android-built"
MAC_EXAMPLES = {
    "bcore build": (None, {0}), "bcore build --offline": (None, {0}), "bcore build --plan": (None, {0}),
    "bcore build -C Custom": (None, {0}), "bcore test brave_unit_tests": (None, {0}),
    "bcore test mac brave_browser_tests --filter 'Example.*'": (None, {0}),
    "bcore test brave_browser_tests -- --gtest_repeat=2": (None, {0}),
    "bcore run": (BUILT, {0}),
    "bcore run --artifact ./out/Custom/'Brave Browser Development.app'": (BUILT_CUSTOM, {0}),
    "bcore build-run": (None, {0}),
    "bcore sync": (None, {0}), "bcore sync android --plan": (None, {0}), "bcore sync mac,android --force": (None, {0}),
    "bcore drift": (None, {0}), "bcore drift --diff": (None, {0}), "bcore patches update": (None, {0}),
    "bcore clean": (BUILT, {0}), "bcore clean android --configuration debug": (None, {0}),
    "bcore clean mac --arch arm64 --execute": (BUILT, {0}), "bcore clean all --execute": (BUILT, {0}),
    "scripts/bcore doctor signing": (None, {0, 3}),
}
ANDROID_EXAMPLES = {
    "bcore android setup --source <url-or-path>": (None, {0}),
    "bcore build android": (ANDROID, {0}), "bcore build android --offline": (ANDROID, {0}),
    "bcore build android -C Custom": (ANDROID, {0}),
    "bcore run android --device <id>": (ANDROID_BUILT, {0}), "bcore deploy android": (ANDROID_BUILT, {0}),
    "bcore build-run android --device <id>": (ANDROID, {0}),
    "bcore build-run android --all-devices": (ANDROID, {0}),
    "bcore run android --all-devices": (ANDROID_BUILT, {0}),
}
GETTING_STARTED = ["scripts/bcore setup", "scripts/bcore checkout add main /work/browser/_bad_scm/workspace/src/brave",
                   "scripts/bcore env init --checkout main", "scripts/bcore doctor mac --checkout main",
                   "scripts/bcore context --checkout main", "scripts/bpm --checkout main run --help"]


def documented_examples():
    found = {}
    for document in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]:
        for body in FENCE.findall(document.read_text(encoding="utf-8")):
            for line in body.splitlines():
                command = re.sub(r"\s+#.*$", "", line.strip())
                if COMMAND.match(command):
                    found.setdefault(command, set()).add(document.name)
    return found


GROUPS = {"android": {"setup"}, "env": {"init", "export", "check"}, "checkout": {"add", "list"},
          "patches": {"update"}, "tools": {"setup"}}


def with_selectors(command, config, checkout=True):
    """Insert the fixture's configuration (and checkout) right after the command words; returns (tool, arguments)."""
    tokens = shlex.split(command)
    tool, rest = tokens[0].rsplit("/", 1)[-1], tokens[1:]
    position = 0 if tool == "bpm" else (2 if len(rest) > 1 and rest[1] in GROUPS.get(rest[0], ()) else 1)
    extra = ["--config", config] + (["--checkout", "main"] if checkout and "--checkout" not in rest else [])
    return tool, [*rest[:position], *extra, *rest[position:]]


class CoverageTests(unittest.TestCase):
    def test_every_documented_command_is_run_here_or_has_a_reason_not_to_be(self):
        classified = set(NOT_RUN) | set(MAC_EXAMPLES) | set(ANDROID_EXAMPLES) | set(GETTING_STARTED) | \
            {"scripts/bpm --checkout main run --help"}
        unclassified = sorted(set(documented_examples()) - classified)
        self.assertEqual(unclassified, [], "document each new example here, or run it")
        stale = sorted(classified - set(documented_examples()))
        self.assertEqual(stale, [], "an example listed here is no longer in the guides")


def run_example(test, command, expected, substitutions=None, cwd=None):
    for placeholder, value in (substitutions or {}).items():
        command = command.replace(placeholder, value)
    tool, arguments = with_selectors(command, test.config)
    result = test.sandbox.bcore(*arguments, tool=tool, cwd=cwd, env=test.env())
    test.assertIn(result.returncode, expected, "%s\n%s\n%s" % (command, result.stdout[-600:], result.stderr[-1200:]))
    return result


def prepare(test, state):
    if state in (BUILT, BUILT_CUSTOM):
        assert test.document("build")[0].returncode == 0
    if state == BUILT_CUSTOM:
        assert test.document("build", "-C", "Custom")[0].returncode == 0


def _mac_test(command, state, expected):
    def test(self):
        prepare(self, state)
        calls = len(self.node_calls())
        result = run_example(self, command, expected, cwd=self.src)
        new_calls = [call["argv"][1:] for call in self.node_calls()[calls:]]
        if command in ("bcore build --plan", "bcore sync android --plan"):
            self.assertIn("nothing was run", result.stdout)
            self.assertEqual(new_calls, [], "a plan runs nothing")
        elif command == "bcore build":
            self.assertTrue(self.output_app().is_dir())
        elif command == "bcore build -C Custom":
            self.assertTrue(self.output_app("Custom").is_dir())
        elif command.startswith("bcore test"):
            words = shlex.split(command)
            suite = next(word for word in words if word.endswith("_tests"))
            self.assertEqual(new_calls[-1][:3], ["run", "test", suite])
            if "--filter" in words:
                self.assertIn("--filter=" + words[words.index("--filter") + 1], new_calls[-1])
            if "--gtest_repeat=2" in words:
                self.assertEqual(new_calls[-1][-2], "--gtest_repeat=2", "forwarded arguments keep their place")
                self.assertTrue(new_calls[-1][-1].startswith("--test-launcher-summary-output="))
        elif command.startswith("bcore run"):
            self.assertTrue([r for r in self.sandbox.records() if r["tool"] == "open"], "the application was launched")
        elif command in ("bcore clean mac --arch arm64 --execute", "bcore clean all --execute"):
            self.assertFalse(self.output_app().parent.exists(), "the previewed output was deleted")
        elif command == "bcore clean":
            self.assertTrue(self.output_app().is_dir(), "a preview deletes nothing")
    return test


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class MacExamples(BuildTestCase):
    pass


for index, (example, (state, expected)) in enumerate(MAC_EXAMPLES.items()):
    setattr(MacExamples, "test_%02d_%s" % (index, re.sub(r"\W+", "_", example)[:40]), _mac_test(example, state, expected))


def _android_test(command, state, expected):
    def test(self):
        substitutions = {"<url-or-path>": str(self.support), "<id>": "emulator-5554"}
        if state:
            self.assertEqual(self.setup_support().returncode, 0)
            self.sandbox.configure_rbe("main")
            with open(self.src.parent / ".gclient", "a") as stream:
                stream.write("target_os = ['android']\n")
        if state == ANDROID_BUILT:
            self.assertEqual(self.document("build", "android")[0].returncode, 0)
        self.sandbox.record.unlink(missing_ok=True)
        env_devices = "emulator-5554,device"
        self.env_extra = {"FAKE_ADB_DEVICES": env_devices}
        run_example(self, command, expected, substitutions)
    return test


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class AndroidExamples(AndroidTestCase):
    def env(self, **extra):
        return super().env(FAKE_ADB_DEVICES="emulator-5554,device", **extra)


for index, (example, (state, expected)) in enumerate(ANDROID_EXAMPLES.items()):
    setattr(AndroidExamples, "test_%02d_%s" % (index, re.sub(r"\W+", "_", example)[:40]),
            _android_test(example, state, expected))


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class GettingStartedExamples(SandboxTest):
    def test_the_first_steps_work_in_order_from_a_fresh_installation(self):
        core = self.sandbox.make_checkout("main")
        config = str(self.sandbox.root / "config" / "fresh.toml")
        self.config = config
        for command in GETTING_STARTED:
            with self.subTest(command=command):
                command = command.replace("/work/browser/_bad_scm/workspace/src/brave", str(core))
                tool, arguments = with_selectors(command, config, checkout=False)
                result = self.sandbox.bcore(*arguments, tool=tool)
                if command.startswith("scripts/bcore env init"):
                    self.sandbox.approve("main")
                self.assertEqual(result.returncode, 0, "%s\n%s%s" % (command, result.stdout[-400:], result.stderr[-800:]))
        self.assertTrue(Path(config).is_file())


if __name__ == "__main__":
    unittest.main()
