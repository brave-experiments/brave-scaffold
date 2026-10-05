# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""iOS Simulator selection and project facts."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests.support  # noqa: F401
from scaffold.brave import cmd_ios, ios
from scaffold.brave.records import OutputState
from scaffold.common.cli import Parsed
from scaffold.common.results import Result, ScaffoldError
from tests.ios_fixtures import bootstrap, make_ios_project, simulator_json


def devices(**kwargs):
    return ios.parse_simulators(json.loads(simulator_json(**kwargs)))[1]


class SimulatorSelectionTests(unittest.TestCase):
    def test_unavailable_devices_and_other_runtimes_are_not_listed(self):
        self.assertEqual(sorted(d["name"] for d in devices()), ["iPad Pro", "iPhone 16", "iPhone 16 Pro", "iPhone 8"])

    def test_default_prefers_a_booted_iphone_then_the_newest_runtime_phone(self):
        self.assertEqual(ios.select_simulator(devices(booted=True), None, "17.0")["udid"], "BBBB-PHONE")
        self.assertEqual(ios.select_simulator(devices(), None, "17.0")["name"], "iPhone 16")

    def test_runtimes_older_than_the_deployment_target_are_not_chosen(self):
        with self.assertRaises(ScaffoldError) as caught:
            ios.select_simulator(devices(old_only=True), None, "17.0")
        self.assertEqual(caught.exception.code, "DEVICE_UNAVAILABLE")
        self.assertEqual(ios.select_simulator(devices(old_only=True), None, None)["udid"], "EEEE-OLD")

    def test_requested_simulators_match_by_name_or_udid_exactly(self):
        self.assertEqual(ios.select_simulator(devices(), "iPhone 16 Pro", "17.0")["udid"], "CCCC-PHONE")
        self.assertEqual(ios.select_simulator(devices(), "AAAA-IPAD", "17.0")["name"], "iPad Pro")
        for requested in ("iPhone", "iPhone 8"):
            with self.assertRaises(ScaffoldError) as caught:
                ios.select_simulator(devices(), requested, "17.0")
            self.assertEqual(caught.exception.code, "DEVICE_UNAVAILABLE")

    def test_a_repeated_name_must_be_disambiguated_by_udid(self):
        twin = devices() + [dict(devices()[0], udid="ZZZZ-IPAD")]
        with self.assertRaises(ScaffoldError) as caught:
            ios.select_simulator(twin, "iPad Pro", "17.0")
        self.assertEqual(caught.exception.code, "DEVICE_AMBIGUOUS")

    def test_physical_devices_are_refused(self):
        for requested in ("iphoneos", "My Physical iPhone", "device"):
            with self.assertRaises(ScaffoldError) as caught:
                ios.select_simulator(devices(), requested, "17.0")
            self.assertEqual(caught.exception.code, "INVALID_INPUT")


class ProjectTests(unittest.TestCase):
    def test_deployment_target_is_the_highest_in_the_project(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Path(directory) / "src" / "brave"
            make_ios_project(core, "17.4", bootstrapped=False)
            project = ios.project_path(SimpleNamespace(core=core, src=core.parent)) / "project.pbxproj"
            self.assertEqual(ios.deployment_target(project), "17.4")
            self.assertIsNone(ios.deployment_target(project.parent / "missing"))

    def test_bootstrap_artifacts_are_reported_until_core_creates_them(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Path(directory) / "src" / "brave"
            identity = SimpleNamespace(core=core, src=core.parent)
            make_ios_project(core, bootstrapped=False)
            self.assertEqual(len(ios.missing_bootstrap_artifacts(identity)), 5)
            bootstrap(core)
            self.assertEqual(ios.missing_bootstrap_artifacts(identity), [])

    def test_output_directory_follows_core_naming(self):
        self.assertEqual(ios.output_directory_name("Debug"), "ios_Debug_arm64_simulator")


class BuildSelectionTests(unittest.TestCase):
    def test_output_assignments_and_configuration_files_leave_the_artifact_unresolved(self):
        identity = SimpleNamespace(core=Path("/checkout/src/brave"), src=Path("/checkout/src"))
        cases = (["CONFIGURATION_BUILD_DIR=/other"], ["SYMROOT=/other"], ["PRODUCT_NAME=Other"],
                 ["CUSTOM_PRODUCTS=/other"], ["ARCHS[sdk=iphonesimulator*]=x86_64"],
                 ["-xcconfig", "/other/settings.xcconfig"])
        for forwarded in cases:
            with self.subTest(forwarded=forwarded):
                build = ios.resolve_build(Parsed(forwarded=forwarded), identity)
                self.assertTrue(build.unresolved)
                self.assertTrue(build.changes_output)
                argv = ios.xcodebuild_argv(build)
                self.assertEqual(argv[-len(forwarded)-1:], [*forwarded, "build"])


class RunFreshnessTests(unittest.TestCase):
    def test_a_current_selected_output_does_not_inherit_another_outputs_uncertainty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = SimpleNamespace(core=root / "src" / "brave", src=root / "src")
            fingerprint = {"core_head": "unchanged"}
            selected = None
            for name in ("default", "custom"):
                output = root / name
                artifact = {"path": str(ios.app_in(output)), "target": "ios", "output_dir": str(output)}
                state = OutputState(identity, output, root)
                state.record_success("build-" + name, artifact, fingerprint)
                if name == "default":
                    state.begin_attempt("failed-rebuild")
                    state.end_attempt("failed-rebuild", "failed")
                else:
                    selected = artifact
            ctx = SimpleNamespace(state_root=root, log=Mock())
            result = Result(command="run")
            patch_plan = SimpleNamespace(report=SimpleNamespace(patched_paths={}))
            with patch.object(cmd_ios.patches, "plan_patch_preparation", return_value=patch_plan), \
                    patch.object(cmd_ios.freshness, "compute", return_value=fingerprint):
                cmd_ios.warn_run_freshness(ctx, identity, selected, result)
            self.assertEqual(result.warnings, [])
            ctx.log.phase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
