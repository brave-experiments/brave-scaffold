# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Completion notifications: policy selection, classification, outcomes, and delivery failure."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401  (puts the source tree on sys.path)
from scaffold.brave import bcore
from scaffold.brave.registry import REGISTRY
from scaffold.common import notify
from scaffold.common.config import load_config
from scaffold.common.results import Cancelled, Result, ScaffoldError


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, title, body):
        if self.fail:
            raise OSError("no notification service")
        self.sent.append((title, body))


class FakeBell:
    def __init__(self, error=None):
        self.rings = 0
        self.error = error

    def ring(self):
        self.rings += 1
        if self.error:
            raise self.error


class NotificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def config(self, policy=None, delivery=None):
        path = self.root / "brave-scaffold.toml"
        text = "schema_version = 1\n"
        if policy is not None or delivery is not None:
            text += "[notifications]\n"
        if policy is not None:
            text += 'policy = "%s"\n' % policy
        if delivery is not None:
            text += 'delivery = "%s"\n' % delivery
        path.write_text(text)
        return str(path)

    def run_cli(self, argv, policy=None, handler=None, notifier=None, delivery="desktop", bell=None):
        """Use desktop delivery for message/policy assertions; pass None to exercise the real default."""
        notifier = notifier or FakeNotifier()
        argv = list(argv) + ["--config", self.config(policy, delivery)]
        out, err = io.StringIO(), io.StringIO()
        spec, _ = bcore.resolve_command(argv)
        handler = handler or (lambda ctx: Result(command=spec.name, text="done"))
        with mock.patch.object(spec, "handler", handler):
            code = bcore.main(argv, stdout=out, stderr=err, notifier=notifier, bell=bell)
        return code, out.getvalue(), err.getvalue(), notifier

    def test_configuration_policy_values(self):
        for policy in ("always", "major", "never"):
            self.assertEqual(load_config(self.config(policy)).notification_policy, policy)
        self.assertIsNone(load_config(self.config()).notification_policy)
        with self.assertRaises(ScaffoldError):
            load_config(self.config("sometimes"))
        path = Path(self.config())
        path.write_text('schema_version = 1\n[notifications]\nlevel = "major"\n')
        with self.assertRaises(ScaffoldError):
            load_config(str(path))

    def test_default_is_major_when_omitted(self):
        _, _, _, fake = self.run_cli(["build"])
        self.assertEqual(len(fake.sent), 1)
        _, _, _, fake = self.run_cli(["doctor"])
        self.assertEqual(fake.sent, [])

    def test_cli_overrides_configuration(self):
        self.assertEqual(len(self.run_cli(["doctor", "--notify"], policy="never")[3].sent), 1)
        self.assertEqual(len(self.run_cli(["doctor", "--notify=always"], policy="never")[3].sent), 1)
        self.assertEqual(self.run_cli(["build", "--notify=never"], policy="always")[3].sent, [])
        self.assertEqual(len(self.run_cli(["cd", "x", "--notify=always"], policy="major")[3].sent), 1)
        self.assertEqual(len(self.run_cli(["build", "--notify=major"], policy="never")[3].sent), 1)

    def test_configuration_applies_without_cli_option(self):
        self.assertEqual(self.run_cli(["build"], policy="never")[3].sent, [])
        self.assertEqual(len(self.run_cli(["doctor"], policy="always")[3].sent), 1)

    def test_invalid_policy_is_rejected_before_the_handler_runs(self):
        ran = []
        code, _, err, fake = self.run_cli(["build", "--notify=sometimes"],
                                          handler=lambda ctx: ran.append(1) or Result(command="build"))
        self.assertEqual((code, ran, fake.sent), (2, [], []))
        self.assertIn("--notify", err)

    def test_bare_notify_does_not_consume_the_next_word(self):
        code, _, _, fake = self.run_cli(["sync", "--notify", "mac"])
        self.assertEqual((code, len(fake.sent)), (0, 1))

    def test_major_operations_notify_and_others_do_not(self):
        major = ["sync", "build", "build-run", "sb", "sbr", "run", "patches update",
                 "setup", "tools setup", "env init", "android setup", "clean --execute"]
        quiet = ["cd x", "context", "doctor", "checkout list", "checkout add a b", "drift", "env check",
                 "capabilities", "clean", "shell"]
        for command in major:
            with self.subTest(command=command):
                self.assertEqual(len(self.run_cli(command.split())[3].sent), 1)
        for command in ("test mac brave_unit_tests", "deploy android"):
            with self.subTest(command=command):
                self.assertEqual(len(self.run_cli(command.split())[3].sent), 1)
        for command in quiet:
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command.split())[3].sent, [])
                self.assertEqual(len(self.run_cli(command.split() + ["--notify=always"])[3].sent),
                                 0 if command == "clean" else 1)

    def test_capabilities_honors_configured_policy_without_requiring_configuration(self):
        fake = FakeNotifier()
        config = self.config("always", "desktop")
        code = bcore.main(["capabilities", "--config", config], stdout=io.StringIO(), stderr=io.StringIO(),
                         notifier=fake)
        self.assertEqual((code, len(fake.sent)), (0, 1))
        fake = FakeNotifier()
        bcore.main(["capabilities", "--config", str(self.root / "missing.toml")], stdout=io.StringIO(),
                  stderr=io.StringIO(), notifier=fake)
        self.assertEqual(fake.sent, [])
        bell = FakeBell()
        bcore.main(["capabilities", "--config", str(self.root / "missing.toml"), "--notify"],
                  stdout=io.StringIO(), stderr=io.StringIO(), notifier=fake, bell=bell)
        self.assertEqual((len(fake.sent), bell.rings), (0, 1))

    def test_previews_pure_exports_and_help_stay_silent_under_always(self):
        for argv in (["build", "--plan"], ["sb", "--plan"], ["env", "export"], ["clean"], ["run", "--plan"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv + ["--notify=always"])[3].sent, [])
        fake = FakeNotifier()
        out = io.StringIO()
        bcore.main(["build", "--help", "--notify=always"], stdout=out, stderr=io.StringIO(), notifier=fake)
        bcore.main(["--help"], stdout=io.StringIO(), stderr=io.StringIO(), notifier=fake)
        self.assertEqual(fake.sent, [])

    def test_combined_command_sends_one_notification_with_the_final_outcome(self):
        def handler(ctx):
            ctx.log.phase("Sync")
            ctx.log.phase("Build")
            raise ScaffoldError("LAUNCH_FAILED", "secret-looking child output --flag=abc")
        code, _, _, fake = self.run_cli(["sbr", "--notify=always"], handler=handler)
        self.assertEqual(code, 5)
        self.assertEqual(len(fake.sent), 1)
        title, body = fake.sent[0]
        self.assertIn("sync-build-run failed", title)
        self.assertIn("LAUNCH_FAILED", body)
        self.assertNotIn("secret-looking", title + body)

    def test_success_text_and_contents(self):
        _, _, _, fake = self.run_cli(["build"])
        title, body = fake.sent[0]
        self.assertEqual(title, "bcore build succeeded")
        self.assertIn("Elapsed:", body)
        self.assertIn("Log: ", body)
        self.assertIn(".bcore", body)

    def test_checkout_name_appears(self):
        def handler(ctx):
            result = Result(command="build")
            result.context = {"checkout": "/work/src/brave", "alias": "main", "selection_source": "cwd"}
            return result
        self.assertIn("Checkout: main", self.run_cli(["build"], handler=handler)[3].sent[0][1])

    def test_cancellation_is_reported(self):
        def handler(ctx):
            raise Cancelled(130)
        code, _, _, fake = self.run_cli(["build"], handler=handler)
        self.assertEqual(code, 130)
        self.assertIn("build cancelled", fake.sent[0][0])
        self.assertIn("exit 130", fake.sent[0][1])

    def test_child_failure_is_reported_with_exit_code(self):
        def handler(ctx):
            return Result(command="test", status="error", exit_code=5)
        self.assertIn("test failed", self.run_cli(["test", "mac", "brave_unit_tests"], handler=handler)[3].sent[0][0])

    def test_delivery_failure_keeps_exit_status_and_structured_stdout(self):
        def handler(ctx):
            return Result(command="build", data={"value": 1})
        code, out, err, fake = self.run_cli(["build", "--json"], handler=handler, notifier=FakeNotifier(fail=True))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"value": 1})
        self.assertIn("Notification not delivered", err)
        self.assertNotIn("Notification", out)

    def test_delivery_failure_keeps_a_failing_exit_status(self):
        def handler(ctx):
            raise ScaffoldError("CHILD_FAILED", "failed")
        code, *_ = self.run_cli(["build"], handler=handler, notifier=FakeNotifier(fail=True))
        self.assertEqual(code, 5)

    def test_delivery_configuration_and_default(self):
        self.assertIsNone(load_config(self.config("major")).notification_delivery)
        self.assertEqual(notify.effective_delivery(load_config(self.config())), "bell")
        for delivery in ("desktop", "bell", "both"):
            self.assertEqual(load_config(self.config(delivery=delivery)).notification_delivery, delivery)
        for bad in ("sound", "Desktop", ""):
            with self.assertRaises(ScaffoldError):
                load_config(self.config(delivery=bad))
        path = Path(self.config())
        path.write_text('schema_version = 1\n[notifications]\ndelivery = 1\n')
        with self.assertRaises(ScaffoldError):
            load_config(str(path))

    def test_delivery_selects_methods_once_each(self):
        for delivery, desktop, rings in (("desktop", 1, 0), ("bell", 0, 1), ("both", 1, 1), (None, 0, 1)):
            with self.subTest(delivery=delivery):
                bell = FakeBell()
                fake = self.run_cli(["sbr"], delivery=delivery, bell=bell)[3]
                self.assertEqual((len(fake.sent), bell.rings), (desktop, rings))

    def test_delivery_does_not_change_when_notifications_fire(self):
        for delivery in ("desktop", "bell", "both"):
            with self.subTest(delivery=delivery):
                bell = FakeBell()
                self.run_cli(["doctor"], delivery=delivery, bell=bell)
                self.run_cli(["build", "--notify=never"], delivery=delivery, bell=bell)
                self.run_cli(["build", "--plan", "--notify=always"], delivery=delivery, bell=bell)
                self.assertEqual(bell.rings, 0)
                self.run_cli(["doctor", "--notify"], delivery=delivery, bell=bell)
                self.assertEqual(bell.rings, 1 if delivery != "desktop" else 0)

    def test_bell_is_one_per_combined_invocation_even_on_failure(self):
        def handler(ctx):
            ctx.log.phase("Sync")
            ctx.log.phase("Build")
            raise ScaffoldError("LAUNCH_FAILED", "failed")
        bell = FakeBell()
        code, *_ = self.run_cli(["sbr"], handler=handler, delivery="bell", bell=bell)
        self.assertEqual((code, bell.rings), (5, 1))

    def test_both_methods_fail_independently(self):
        bell = FakeBell()
        _, _, err, _ = self.run_cli(["build"], delivery="both", bell=bell, notifier=FakeNotifier(fail=True))
        self.assertEqual(bell.rings, 1)
        self.assertIn("Notification not delivered", err)
        fake = FakeNotifier()
        code, _, err, _ = self.run_cli(["build"], delivery="both", notifier=fake,
                                       bell=FakeBell(error=OSError("write failed")))
        self.assertEqual((code, len(fake.sent)), (0, 1))
        self.assertIn("Terminal bell not delivered", err)

    def test_missing_terminal_skips_the_bell_without_substitution(self):
        bell = FakeBell(error=notify.NoTerminal("no tty"))
        fake = FakeNotifier()
        code, out, err, _ = self.run_cli(["build"], delivery="bell", notifier=fake, bell=bell)
        self.assertEqual((code, bell.rings, fake.sent, "bell" in err.lower()), (0, 1, [], False))
        bell = FakeBell(error=notify.NoTerminal("no tty"))
        fake = FakeNotifier()
        self.run_cli(["build"], delivery="both", notifier=fake, bell=bell)
        self.assertEqual((bell.rings, len(fake.sent)), (1, 1))

    def test_bell_leaves_streams_logs_and_exit_status_unchanged(self):
        def handler(ctx):
            return Result(command="build", data={"value": 1}, text="done")
        results = {}
        for delivery, bell in (("desktop", None), ("bell", FakeBell())):
            code, out, err, _ = self.run_cli(["build", "--json"], handler=handler, delivery=delivery,
                                             bell=bell, notifier=FakeNotifier())
            log = next((self.root / ".bcore" / "logs").glob("*.log")).read_text()
            for path in (self.root / ".bcore" / "logs").glob("*.log"):
                path.unlink()
            results[delivery] = (code, out, err.splitlines()[:-1], log)
        self.assertEqual(results["bell"][:2], results["desktop"][:2])
        for value in results["bell"][1:]:
            self.assertNotIn("\a", str(value))
        self.assertNotIn("ell", results["bell"][3])

    def test_terminal_bell_writes_bel_to_the_terminal_device_only(self):
        device = self.root / "tty"
        device.write_bytes(b"")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(notify, "TERMINAL_DEVICE", str(device)), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            notify.TerminalBell().ring()
        self.assertEqual((device.read_bytes(), out.getvalue(), err.getvalue()), (b"\a", "", ""))

    def test_terminal_bell_without_a_terminal_is_skipped(self):
        with mock.patch.object(notify, "TERMINAL_DEVICE", str(self.root / "absent" / "tty")):
            with self.assertRaises(notify.NoTerminal):
                notify.TerminalBell().ring()
            self.assertIsNone(notify.deliver_bell(notify.TerminalBell()))

    def test_bell_backend_can_be_disabled(self):
        self.assertIsNone(notify.default_bell({notify.BACKEND_VARIABLE: "none"}))
        self.assertIsInstance(notify.default_bell({}), notify.TerminalBell)

    def test_macos_backend_passes_text_as_arguments(self):
        with mock.patch("subprocess.run") as run:
            notify.MacNotifier().send('T"itle', 'body" & do shell script "x"')
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-2:], ['body" & do shell script "x"', 'T"itle'])
        self.assertFalse(any("do shell script" in part for part in argv[1:-2]))

    def test_backend_can_be_disabled_and_failures_are_contained(self):
        self.assertIsNone(notify.default_notifier({notify.BACKEND_VARIABLE: "none"}))
        self.assertIn("OSError", notify.deliver(FakeNotifier(fail=True), "t", "b"))


if __name__ == "__main__":
    unittest.main()
