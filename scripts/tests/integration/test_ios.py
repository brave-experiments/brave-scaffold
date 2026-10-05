# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""iOS Simulator sync and readiness with fake Xcode tools and a fake package command."""

import unittest

from tests.integration.test_build import SKIP, BuildTestCase
from tests.ios_fixtures import install_fake_xcode, make_ios_project, simulator_json

IOS_HOOK = """
if "sync" in argv:
    requested = [a.split("=", 1)[1] for a in argv if a.startswith("--target_os=")]
    gclient = os.path.join(os.path.dirname(os.environ["BRAVE_CORE_DIR"]), "..", ".gclient")
    with open(gclient, "a") as stream:
        stream.write("target_os = %r\\n" % (requested[0].split(",") if requested else [],))
    if not os.environ.get("FAKE_NO_BOOTSTRAP") and "ios" in requested[0].split(","):
        core = os.environ["BRAVE_CORE_DIR"]
        link = os.path.join(os.path.dirname(core), "out", "ios_current_link")
        for name in ("BraveCore", "NalaAssets", "PartitionAllocSupport"):
            os.makedirs(os.path.join(link, name + ".xcframework"), exist_ok=True)
            open(os.path.join(link, name + ".xcframework", "Info.plist"), "w").write("<plist/>")
        open(os.path.join(link, "args.xcconfig"), "w").write("")
        configuration = os.path.join(core, "ios", "brave-ios", "App", "Configuration")
        os.makedirs(configuration, exist_ok=True)
        open(os.path.join(configuration, "LLDBInit"), "w").write("")
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class IosTestCase(BuildTestCase):
    bootstrapped = True

    def setUp(self):
        super().setUp()
        install_fake_xcode(self.sandbox)
        make_ios_project(self.core, bootstrapped=self.bootstrapped)
        with open(self.src.parent / ".gclient", "a") as stream:
            stream.write("target_os = ['ios']\n")
        self.hook = self.sandbox.hook(IOS_HOOK)

    def env(self, **extra):
        return super().env(**{"FAKE_SIMCTL_JSON": simulator_json(), **extra})

    def statuses(self, document):
        return {check["name"]: check["status"] for check in document["checks"]}

    def doctor(self, **extra):
        result, document = self.document("doctor", "ios", env=self.env(**extra))
        return result, document, self.statuses(document)


class IosSyncTests(IosTestCase):
    bootstrapped = False

    def test_sync_adds_ios_to_the_existing_targets_and_checks_core_bootstrapped_the_project(self):
        (self.src.parent / ".gclient").write_text("target_os = ['android']\n")
        result, document = self.document("sync", "ios")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", "--target_os=android,ios"])

    def test_sync_fails_with_the_bootstrap_repair_when_core_leaves_the_project_unbootstrapped(self):
        result, document = self.document("sync", "ios", env=self.env(FAKE_NO_BOOTSTRAP="1"))
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"))
        self.assertIn(["bdev", "bpm", "run", "ios_bootstrap"], [r["argv"][:4] for r in document["error"]["repairs"]])

    def test_sync_ios_refuses_options_that_defeat_the_bootstrap_hook_or_the_target_list(self):
        for option in ("--nohooks", "--target_os=ios"):
            result, document = self.document("sync", "ios", option)
            self.assertEqual(document["error"]["code"], "SELECTOR_CONFLICT", option)
        self.assertEqual(self.node_calls(), [])

    def test_sync_combines_mobile_targets_in_a_fixed_order(self):
        result, document = self.document("sync", "ios,android")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.build_argv(), ["run", "sync", "--target_os=android,ios"])

    def test_sync_plan_names_the_arguments_and_runs_nothing(self):
        result, document = self.document("sync", "ios", "--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--target_os=ios", document["data"]["plan"]["argv_arguments"])
        self.assertEqual(self.node_calls(), [])


class IosDoctorTests(IosTestCase):
    def test_a_bootstrapped_checkout_with_a_compatible_simulator_is_ready(self):
        result, document, statuses = self.doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("ios-xcodebuild", "ios-simulator-sdk", "ios-gclient-target", "ios-project", "ios-bootstrap",
                     "ios-simulator"):
            self.assertEqual(statuses[name], "pass", name)

    def test_each_missing_prerequisite_is_a_blocker_with_its_own_check(self):
        for extra, name in ((dict(FAKE_NO_XCODEBUILD="1"), "ios-xcodebuild"),
                            (dict(FAKE_NO_SIMULATOR_SDK="1"), "ios-simulator-sdk"),
                            (dict(FAKE_SIMCTL_FAIL="1"), "ios-simulator"),
                            (dict(FAKE_SIMCTL_JSON=simulator_json(old_only=True)), "ios-simulator")):
            with self.subTest(name=name, extra=list(extra)):
                result, document, statuses = self.doctor(**extra)
                self.assertEqual((result.returncode, statuses[name]), (3, "blocker"))

    def test_an_unbootstrapped_checkout_gets_the_bootstrap_repair(self):
        import shutil
        shutil.rmtree(self.src / "out" / "ios_current_link")
        result, document, statuses = self.doctor()
        self.assertEqual(statuses["ios-bootstrap"], "blocker")
        check = next(c for c in document["checks"] if c["name"] == "ios-bootstrap")
        self.assertEqual(check["repairs"][0]["argv"][:4], ["bdev", "bpm", "run", "ios_bootstrap"])

    def test_a_checkout_without_an_ios_target_gets_the_sync_repair(self):
        (self.src.parent / ".gclient").write_text("target_os = []\n")
        result, document, statuses = self.doctor()
        self.assertEqual(statuses["ios-gclient-target"], "blocker")
        check = next(c for c in document["checks"] if c["name"] == "ios-gclient-target")
        self.assertEqual(check["repairs"][0]["argv"][:3], ["bdev", "sync", "ios"])


if __name__ == "__main__":
    unittest.main()
