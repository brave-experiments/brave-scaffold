# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Signer protocol behavior, Git signing procedure, and signing doctor scope."""

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import SCRIPTS, SandboxTest, write_executable

from scaffold.brave import signer

SRC = str(SCRIPTS / "src")


def result(code=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def harness(directory, signer_path, timeout=None):
    """A Git-callable program that runs the real signer against a substitute private-key provider."""
    lines = ["#!%s" % sys.executable, "import sys", "sys.path.insert(0, %r)" % SRC,
             "from scaffold.brave import signer", "signer.SIGNER = %r" % str(signer_path)]
    if timeout is not None:
        lines.append("signer.TIMEOUT_SECONDS = %r" % timeout)
    lines.append("sys.exit(signer.main(sys.argv[1:]))")
    path = Path(directory) / "signer harness"
    write_executable(path, "\n".join(lines) + "\n")
    return path


class RecoveryPolicyTests(unittest.TestCase):
    failure = result(1, stderr=signer.SOCKET_ERROR)

    def test_success_and_denial_neither_open_the_app_nor_retry(self):
        for response in (result(stdout=b"signature"), result(1, stderr=b"User denied authentication")):
            with mock.patch.object(signer, "run", return_value=response) as run, \
                    mock.patch.object(signer, "can_open_app") as probe:
                self.assertIs(signer.sign(["-Y", "sign", "f"], time.monotonic() + 10), response)
                self.assertEqual(run.call_count, 1)
                probe.assert_not_called()

    def test_missing_agent_retries_exactly_once_with_identical_arguments(self):
        arguments = ["-Y", "sign", "-f", "key with spaces", "commit file"]
        deadline = time.monotonic() + 15
        with mock.patch.object(signer, "run", side_effect=[self.failure, result(), result(stdout=b"sig")]) as run, \
                mock.patch.object(signer, "can_open_app", return_value=True), mock.patch.object(signer.time, "sleep"):
            self.assertEqual(signer.sign(arguments, deadline).stdout, b"sig")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0], run.call_args_list[2])
        self.assertEqual(run.call_args_list[0].args, ([signer.SIGNER, *arguments], deadline))

    def test_repeated_socket_error_or_failed_launch_stops(self):
        for responses in ([self.failure, result(1)], [self.failure, result(), self.failure]):
            with mock.patch.object(signer, "run", side_effect=responses) as run, \
                    mock.patch.object(signer, "can_open_app", return_value=True), \
                    mock.patch.object(signer.time, "sleep"):
                self.assertIs(signer.sign(["-Y", "sign", "f"], time.monotonic() + 15), self.failure)
                self.assertEqual(run.call_count, len(responses))

    def test_locked_unknown_or_startup_locked_session_never_launches_or_retries(self):
        with mock.patch.object(signer, "run", return_value=self.failure) as run, \
                mock.patch.object(signer, "can_open_app", return_value=False):
            signer.sign(["-Y", "sign", "f"], time.monotonic() + 15)
            self.assertEqual(run.call_count, 1)
        with mock.patch.object(signer, "run", side_effect=[self.failure, result()]) as run, \
                mock.patch.object(signer, "can_open_app", side_effect=[True, False]), \
                mock.patch.object(signer.time, "sleep"):
            self.assertIs(signer.sign(["-Y", "sign", "f"], time.monotonic() + 15), self.failure)
            self.assertEqual(run.call_count, 2)

    def test_session_must_be_an_unlocked_console_owned_by_the_caller(self):
        good = {"IOConsoleLocked": False, "IOConsoleUsers": [
            {"kCGSSessionUserIDKey": 501, "kCGSSessionOnConsoleKey": True, "kCGSessionLoginDoneKey": True}]}
        self.assertTrue(signer.session_is_unlocked(good, 501))
        self.assertFalse(signer.session_is_unlocked(good, 502))
        self.assertFalse(signer.session_is_unlocked(dict(good, IOConsoleLocked=0), 501))
        self.assertFalse(signer.session_is_unlocked({}, 501))

    def test_remote_sessions_never_raise_the_app(self):
        with mock.patch.object(signer.sys, "platform", "darwin"), \
                mock.patch.dict(os.environ, {"SSH_CONNECTION": "x"}), mock.patch.object(signer, "run") as run:
            self.assertFalse(signer.can_open_app(time.monotonic() + 10))
            run.assert_not_called()


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def sleeper(self):
        """A signer stand-in that publishes its pid atomically and starts in milliseconds, whatever the load."""
        pid_file = self.dir / "pid"
        write_executable(self.dir / "child", "#!/bin/sh\necho $$ > %s.tmp && mv %s.tmp %s\nexec sleep 30\n" % (
            (shlex.quote(str(pid_file)),) * 3))
        return self.dir / "child", pid_file

    def assert_dead(self, pid_file):
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_timeout_reports_124_and_kills_the_signer(self):
        child, pid_file = self.sleeper()
        started = time.monotonic()
        process = subprocess.run([str(harness(self.dir, child, timeout=2))], capture_output=True, timeout=20)
        self.assertEqual((process.returncode, process.stdout), (124, b""))
        self.assertIn(b"timed out", process.stderr)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(pid_file.exists(), "the signer stand-in never started, so nothing was proved")
        self.assert_dead(pid_file)

    def test_termination_reports_130_and_kills_the_signer(self):
        child, pid_file = self.sleeper()
        process = subprocess.Popen([str(harness(self.dir, child, timeout=20))], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual((process.returncode, stdout), (130, b""))
        self.assertIn(b"cancelled", stderr)
        self.assert_dead(pid_file)

    def test_stdin_arguments_and_stdout_pass_through_without_an_envelope(self):
        echo = self.dir / "echo"
        write_executable(echo, "#!%s\nimport sys\nsys.stdout.buffer.write(b'|'.join(a.encode() for a in sys.argv[1:])"
                         " + b'\\n' + sys.stdin.buffer.read())\n" % sys.executable)
        process = subprocess.run([str(harness(self.dir, echo)), "-Y", "sign", "a b", ""], input=b"payload",
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"-Y|sign|a b|\npayload")

    def test_failure_keeps_exit_status_and_never_succeeds_silently(self):
        failing = self.dir / "fail"
        write_executable(failing, "#!%s\nimport sys\nsys.stderr.write('denied')\nsys.exit(7)\n" % sys.executable)
        process = subprocess.run([str(harness(self.dir, failing)), "-Y", "sign"], capture_output=True, timeout=10)
        self.assertEqual((process.returncode, process.stdout), (7, b""))
        self.assertIn(b"no further retry", process.stderr)

    def test_launcher_runs_the_signer_entry_point(self):
        process = subprocess.run([str(SCRIPTS / "git-sign-with-1password"), "-Y", "sign", "/nonexistent"],
                                 capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"")
        self.assertNotIn(b"schema_version", process.stdout + process.stderr)


class GitProcedureTests(unittest.TestCase):
    """Signed commit first; a signer failure must never yield an unsigned commit by itself."""

    def setUp(self):
        self.dir = Path(os.path.realpath(tempfile.mkdtemp(prefix="signing test ")))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.com")
        self.key = self.dir / "key"
        self.git("init", "-q")
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True)
        self.git("config", "gpg.format", "ssh")
        self.git("config", "user.signingkey", str(self.key))
        allowed = self.dir / "allowed"
        allowed.write_text("t@example.com " + self.key.with_suffix(".pub").read_text())
        self.git("config", "gpg.ssh.allowedSignersFile", str(allowed))

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.dir, env=self.env, capture_output=True, text=True, check=check)

    def head(self):
        return self.git("rev-parse", "--verify", "-q", "HEAD", check=False).stdout

    def test_signed_commit_verifies_and_failure_leaves_head_alone(self):
        self.git("config", "gpg.ssh.program", str(harness(self.dir, "/usr/bin/ssh-keygen")))
        self.git("commit", "-S", "--allow-empty", "-m", "feat: signed")
        self.git("verify-commit", "HEAD")
        before = self.head()
        self.git("config", "gpg.ssh.program", str(harness(self.dir, "/usr/bin/false")))
        failed = self.git("commit", "-S", "--allow-empty", "-m", "feat: nope", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.head(), before)

    def test_unsigned_marker_commit_is_a_separate_explicit_attempt(self):
        self.git("config", "gpg.ssh.program", str(harness(self.dir, "/usr/bin/false")))
        self.git("config", "commit.gpgsign", "true")
        self.assertNotEqual(self.git("commit", "--allow-empty", "-m", "docs: x", check=False).returncode, 0)
        self.assertEqual(self.head(), "")
        self.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "🚧 docs: x")
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), "🚧 docs: x")
        self.assertNotIn("gpgsig", self.git("cat-file", "commit", "HEAD").stdout)
        self.assertEqual(self.git("config", "commit.gpgsign").stdout.strip(), "true", "policy unchanged")
        self.assertNotEqual(self.git("verify-commit", "HEAD", check=False).returncode, 0)


class SigningDoctorTests(SandboxTest):
    def doctor(self, gitconfig):
        config = self.sandbox.root / "gitconfig"
        config.write_text(gitconfig)
        self.sandbox.write_config([])
        empty = self.sandbox.root / "empty.git"
        subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True)
        env = self.sandbox.env(GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM="1", GIT_DIR=str(empty))
        result = self.sandbox.bcore("--json", "doctor", "signing", "--config", str(self.sandbox.config), env=env)
        return result, json.loads(result.stdout)

    def statuses(self, document):
        return {c["name"]: (c["status"], c["required"]) for c in document["checks"]}

    def test_missing_user_configuration_blocks_with_named_checks(self):
        result, document = self.doctor("")
        self.assertEqual((result.returncode, document["error"]["code"]), (3, "READINESS_BLOCKED"))
        for name in ("git-signing-format", "signer-program", "signing-key"):
            self.assertEqual(self.statuses(document)[name], ("blocker", True))
        self.assertEqual(self.statuses(document)["commit-signing-default"], ("warning", False))

    @unittest.skipUnless(os.access(signer.SIGNER, os.X_OK), "1Password signer not installed")
    def test_configured_signing_passes_and_optional_gaps_only_warn(self):
        program = self.sandbox.root / "signer"
        write_executable(program, "#!/bin/sh\n")
        result, document = self.doctor("[gpg]\nformat = ssh\n[gpg \"ssh\"]\nprogram = %s\n"
                                       "[user]\nsigningkey = key::ssh-ed25519 AAAA\n" % program)
        self.assertEqual((result.returncode, document["status"]), (0, "ok"))
        self.assertIn("CHECK_WARNING", {w["code"] for w in document["warnings"]})
        self.assertNotIn("AAAA", result.stdout, "key material is not echoed")

    def test_doctor_writes_no_git_configuration(self):
        config = self.sandbox.root / "gitconfig"
        self.doctor("[gpg]\nformat = ssh\n")
        self.assertEqual(config.read_text(), "[gpg]\nformat = ssh\n")


if __name__ == "__main__":
    unittest.main()
