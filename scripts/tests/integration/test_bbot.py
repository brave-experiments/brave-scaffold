# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The bbot launcher: checkout selection, the credential environment, builds, and stale warnings."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.support import SCRIPTS, quiet_git_maintenance

# Each fake records its arguments and working directory, one line per call.
FAKE_DIRENV = """#!/bin/sh
echo "direnv $*" >> "$FAKE_LOG"
case ${FAKE_DIRENV:-allowed} in
  blocked) echo "direnv: error $2/.envrc is blocked. Run \\`direnv allow\\` to approve its content" >&2; exit 1 ;;
  blocked-zero) echo "direnv: error $2/.envrc is blocked. Run \\`direnv allow\\` to approve its content" >&2 ;;
  fails) echo "direnv: error exit status 1" >&2; exit 1 ;;
  unblocked-path) echo "direnv: loading /work/unblocked/bravebot/.envrc" >&2 ;;
esac
[ "$1" = exec ] || exit 0
shift 2
BBOT_CREDENTIALS=loaded exec "$@"
"""
FAKE_CARGO = """#!/bin/sh
echo "cargo $* credentials=${BBOT_CREDENTIALS:-none} unconfigured=${BRAVEBOT_ALLOW_UNCONFIGURED_BUILD-unset} cwd=$(pwd -P)" >> "$FAKE_LOG"
[ -n "${FAKE_CARGO_EDITS-}" ] && echo edit > "$(pwd -P)/edited-during-build.txt"
[ -n "${FAKE_CARGO_UNLINKS_WORKTREE-}" ] && rm -f "$(pwd -P)/.git"
echo "   Compiling bravebot (fake)" >&2
manifest=
while [ $# -gt 0 ]; do
  [ "$1" = --manifest-path ] && manifest=$2
  shift
done
out=${CARGO_TARGET_DIR:-${manifest%/Cargo.toml}/target}/release
mkdir -p "$out"
printf '#!/bin/sh\\necho "bravebot $*"\\n' > "$out/bravebot"
chmod +x "$out/bravebot"
echo '{"reason":"build-script-executed","executable":null}'
printf '{"reason":"compiler-artifact","executable":"%s/bravebot"}\\n' "$out"
echo '{"reason":"build-finished","success":true}'
exit "${FAKE_CARGO_EXIT:-0}"
"""
FAKE_CP = """#!/bin/sh
for last; do :; done
[ -n "${FAKE_CP_PARTIAL-}" ] && { echo partial > "$last"; echo "cp: disk full" >&2; exit 1; }
[ -n "${FAKE_CP_FAILS-}" ] && { echo "cp: simulated failure" >&2; exit 1; }
exec /bin/cp "$@"
"""
FAKE_MAKE = """#!/bin/sh
echo "make $* credentials=${BBOT_CREDENTIALS:-none}" >> "$FAKE_LOG"
echo "make output for $3"
[ -n "${FAKE_MAKE_STARTS_APP-}" ] && echo "$FAKE_MAKE_STARTS_APP" > "$FAKE_RUNNING_FILE"
if [ "$3" = app-bundle ] && [ -z "${FAKE_MAKE_NOOP-}" ]; then
  arch=x64
  [ "${FAKE_ARM64:-1}" = 1 ] && arch=arm64
  arch=${FAKE_MAKE_ARCH:-$arch}
  mkdir -p "$2/ui/dist/Brave Bot-darwin-$arch/Brave Bot.app/Contents"
  date > "$2/ui/dist/Brave Bot-darwin-$arch/Brave Bot.app/Contents/stamp"
fi
exit "${FAKE_MAKE_EXIT:-0}"
"""
# Apple Silicon unless told otherwise; FAKE_UNAME_M=x86_64 with FAKE_ARM64=1 is a shell under Rosetta.
FAKE_UNAME = """#!/bin/sh
case $1 in -s) echo Darwin ;; -m) echo "${FAKE_UNAME_M:-arm64}" ;; *) /usr/bin/uname "$@" ;; esac
"""
FAKE_SYSCTL = """#!/bin/sh
[ "$*" = "-n hw.optional.arm64" ] && { echo "${FAKE_ARM64:-1}"; exit 0; }
exec /usr/sbin/sysctl "$@"
"""
FAKE_OPEN = """#!/bin/sh
echo "open $*" >> "$FAKE_LOG"
"""
# Fails to replace the installed terminal app when FAKE_MV_FAILS is set, as an interrupted install would.
FAKE_MV = """#!/bin/sh
for last; do :; done
case $last in */.bbot/bravebot) [ -n "${FAKE_MV_FAILS-}" ] && { echo "mv: interrupted" >&2; exit 1; } ;; esac
exec /bin/mv "$@"
"""
FAKE_DITTO = """#!/bin/sh
[ -n "${FAKE_DITTO_STARTS_APP-}" ] && echo "$FAKE_DITTO_STARTS_APP" > "$FAKE_RUNNING_FILE"
exec /usr/bin/ditto "$@"
"""
# Lists a process from the installed desktop app when FAKE_APP_RUNNING names its bundle.
FAKE_PS = """#!/bin/sh
[ -n "${FAKE_PS_FAILS-}" ] && { echo "ps: not permitted" >&2; exit 1; }
echo "/usr/libexec/some-daemon --flag"
[ -n "${FAKE_APP_RUNNING-}" ] && echo "$FAKE_APP_RUNNING/Contents/MacOS/Brave Bot --type=renderer"
[ -f "$FAKE_RUNNING_FILE" ] && echo "$(cat "$FAKE_RUNNING_FILE")/Contents/MacOS/Brave Bot"
exit 0
"""


@unittest.skipUnless(sys.platform == "darwin", "bbot's desktop build and ditto are macOS-only")
class BbotTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="bbot-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.scaffold = self.temp / "scaffold"
        (self.scaffold / "scripts" / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(SCRIPTS / "bbot", self.scaffold / "scripts" / "bbot")
        (self.scaffold / "scripts" / ".venv" / "bin" / "python").symlink_to(sys.executable)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        for name, text in (("direnv", FAKE_DIRENV), ("cargo", FAKE_CARGO), ("make", FAKE_MAKE), ("open", FAKE_OPEN),
                           ("cp", FAKE_CP), ("uname", FAKE_UNAME), ("sysctl", FAKE_SYSCTL), ("ps", FAKE_PS), ("mv", FAKE_MV),
                           ("ditto", FAKE_DITTO)):
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)
        self.log = self.temp / "calls.log"
        inherited = {key: value for key, value in os.environ.items()
                     if key != "CARGO_TARGET_DIR" and not key.startswith("DIRENV_") and key != "BBOT_SOURCE"}
        self.git_env = {**inherited, "HOME": str(self.temp), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        quiet_git_maintenance(self.git_env)
        self.checkout = self.make_repo(self.scaffold / "bravebot")
        (self.checkout / ".envrc").write_text("export SECRET=example\n")
        self.git(self.checkout, "add", ".envrc")
        self.git(self.checkout, "commit", "-q", "-m", "envrc")

    def make_repo(self, path):
        path.mkdir(parents=True)
        self.git(path, "init", "-q", "-b", "main")
        (path / "Cargo.toml").write_text("[package]\n")
        (path / ".gitignore").write_text("/target/\n/ui/dist/\n")
        self.git(path, "add", ".")
        self.git(path, "commit", "-q", "-m", "init")
        return path

    def git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, env=self.git_env, check=True, capture_output=True)

    def bbot(self, *args, cwd=None, **env):
        environ = {**self.git_env, "PATH": "%s:/usr/bin:/bin:/usr/sbin:/sbin" % self.bin, "FAKE_LOG": str(self.log),
                   "FAKE_RUNNING_FILE": str(self.temp / "running"), **env}
        return subprocess.run([str(self.scaffold / "scripts" / "bbot"), *args], cwd=cwd or self.temp, env=environ,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_build_installs_the_terminal_app_under_the_scaffold_with_the_credentials_loaded(self):
        result = self.bbot("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        binary = self.scaffold / ".bbot" / "bravebot"
        self.assertTrue(os.access(binary, os.X_OK))
        record = (self.scaffold / ".bbot" / "build-info").read_text()
        self.assertIn("source=%s" % self.checkout, record)
        self.assertIn("state=clean", record)
        cargo = next(call for call in self.calls() if call.startswith("cargo"))
        self.assertIn("--manifest-path %s/Cargo.toml --bin bravebot" % self.checkout, cargo)
        self.assertTrue(cargo.endswith("credentials=loaded unconfigured=0 cwd=%s" % self.checkout),
                        "cargo runs in the checkout with the credential check enforced")
        self.assertIn(" --locked ", cargo)
        self.assertEqual(oct((self.scaffold / ".bbot").stat().st_mode & 0o777), "0o700", "the copies hold credentials")
        run = self.bbot("hello", "world")
        self.assertEqual((run.returncode, run.stdout.strip(), run.stderr), (0, "bravebot hello world", ""))

    def test_the_binary_is_taken_from_where_cargo_built_it(self):
        target = self.temp / "shared-target"
        result = self.bbot("build", CARGO_TARGET_DIR=str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.checkout / "target").exists())
        self.assertEqual(self.bbot("x").stdout.strip(), "bravebot x")

    def test_an_interrupted_install_is_reported_until_the_next_build(self):
        self.assertEqual(self.bbot("build").returncode, 0)
        first = (self.scaffold / ".bbot" / "build-info").read_text()
        (self.checkout / "change.txt").write_text("new\n")
        self.git(self.checkout, "add", "change.txt")
        self.git(self.checkout, "commit", "-q", "-m", "change")
        result = self.bbot("build", FAKE_MV_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        record = (self.scaffold / ".bbot" / "build-info").read_text()
        self.assertIn("state=installing", record)
        self.assertEqual([line for line in record.splitlines() if line.startswith("sha=")],
                         [line for line in first.splitlines() if line.startswith("sha=")],
                         "the record keeps describing the copy still in place")
        result = self.bbot("run")
        self.assertIn("did not finish", result.stderr)
        self.assertEqual(self.bbot("build").returncode, 0)
        self.assertNotIn("did not finish", self.bbot("run").stderr)

    def test_status_reports_a_missing_build_record_plainly(self):
        self.assertEqual(self.bbot("build").returncode, 0)
        (self.scaffold / ".bbot" / "build-info").unlink()
        result = self.bbot("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(no build record)", result.stdout)
        self.assertEqual(result.stderr, "")
        result = self.bbot("run")
        self.assertIn("no build record", result.stderr)
        self.assertNotIn("does not match HEAD", result.stderr)

    def test_a_stale_bbot_source_does_not_block_commands_that_do_not_build(self):
        self.assertEqual(self.bbot("build").returncode, 0)
        for args in (["run"], ["status"]):
            with self.subTest(args=args):
                result = self.bbot(*args, BBOT_SOURCE="removed")
                self.assertEqual(result.returncode, 0, result.stderr)
        result = self.bbot("--source", str(self.checkout), "build", BBOT_SOURCE="removed")
        self.assertEqual(result.returncode, 0, "--source wins over BBOT_SOURCE")
        result = self.bbot("build", BBOT_SOURCE="removed")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no worktree 'removed'", result.stderr)

    def test_a_worktree_name_wins_over_a_directory_of_the_same_name_here(self):
        self.git(self.checkout, "worktree", "add", "-q", str(self.checkout / ".worktrees" / "ui"), "-b", "ui")
        (self.temp / "ui").mkdir()
        result = self.bbot("--source", "ui", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source=%s" % (self.checkout / ".worktrees" / "ui"),
                      (self.scaffold / ".bbot" / "build-info").read_text())

    def test_an_app_left_aside_by_an_interrupted_install_is_restored(self):
        self.assertEqual(self.bbot("build", "ui").returncode, 0)
        ui = self.scaffold / ".bbot" / "ui"
        (ui / "Brave Bot.app").rename(ui / "Brave Bot.app.old")
        (self.bin / "ditto").write_text('#!/bin/sh\necho "ditto: simulated failure" >&2\nexit 1\n')
        (self.bin / "ditto").chmod(0o755)
        self.assertNotEqual(self.bbot("build", "ui").returncode, 0)
        self.assertTrue((ui / "Brave Bot.app" / "Contents").is_dir(), "the previous app is back in place")

    def test_the_users_cargo_target_directory_does_not_reach_the_tests(self):
        with unittest.mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": str(self.temp / "outside")}):
            self.setUp()
            self.assertEqual(self.bbot("build").returncode, 0)
        self.assertFalse((self.temp / "outside").exists())

    def test_a_desktop_build_that_writes_no_new_bundle_is_not_installed(self):
        self.assertEqual(self.bbot("build", "ui").returncode, 0)
        before = (self.scaffold / ".bbot" / "ui" / "build-info").read_text()
        result = self.bbot("build", "ui", FAKE_MAKE_NOOP="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not produce a new", result.stderr)
        self.assertIn("refused: ", self.logs()[-1].read_text(), "the reason is in the log too")
        self.assertEqual((self.scaffold / ".bbot" / "ui" / "build-info").read_text().replace("state=installing",
                         "state=clean"), before.replace("state=dirty", "state=clean"))

    def test_a_shell_under_rosetta_still_builds_the_arm64_app(self):
        stale = self.checkout / "ui" / "dist" / "Brave Bot-darwin-x64" / "Brave Bot.app"
        stale.mkdir(parents=True)
        result = self.bbot("build", "ui", FAKE_UNAME_M="x86_64", FAKE_ARM64="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.scaffold / ".bbot" / "ui" / "Brave Bot.app" / "Contents" / "stamp").exists())

    def test_a_bundle_for_another_architecture_is_named_in_the_refusal(self):
        result = self.bbot("build", "ui", FAKE_MAKE_ARCH="x64")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Brave Bot-darwin-x64", result.stderr)

    def test_a_caller_that_allows_unconfigured_builds_cannot_skip_the_credentials(self):
        self.assertEqual(self.bbot("build", BRAVEBOT_ALLOW_UNCONFIGURED_BUILD="1").returncode, 0)
        cargo = next(call for call in self.calls() if call.startswith("cargo"))
        self.assertIn("unconfigured=0 ", cargo)

    def test_a_source_changed_during_the_build_is_recorded_as_dirty(self):
        result = self.bbot("build", FAKE_CARGO_EDITS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state=dirty", (self.scaffold / ".bbot" / "build-info").read_text())

    def test_a_source_that_cannot_be_read_after_the_build_keeps_the_started_commit(self):
        worktree = self.temp / "outside" / "brief"  # outside the checkout, so Git cannot fall back to it
        self.git(self.checkout, "worktree", "add", "-q", str(worktree), "-b", "brief")
        result = self.bbot("--source", str(worktree), "build", FAKE_CARGO_UNLINKS_WORKTREE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = (self.scaffold / ".bbot" / "build-info").read_text()
        self.assertRegex(record, r"sha=[0-9a-f]{40}\n")
        self.assertIn("state=dirty", record)

    def test_a_status_that_cannot_be_read_counts_as_uncommitted_changes(self):
        (self.checkout / ".git" / "index").write_bytes(b"not an index")
        result = self.bbot("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state=dirty", (self.scaffold / ".bbot" / "build-info").read_text())

    def test_a_running_desktop_app_is_never_replaced_underneath_it(self):
        self.assertEqual(self.bbot("build", "ui").returncode, 0)
        app = self.scaffold / ".bbot" / "ui" / "Brave Bot.app"
        stamp = (app / "Contents" / "stamp").read_text()
        for args in (["build", "ui"], ["br", "ui"]):
            with self.subTest(args=args):
                result = self.bbot(*args, FAKE_APP_RUNNING=str(app))
                self.assertEqual(result.returncode, 1)
                self.assertIn("quit Brave Bot first", result.stderr)
                self.assertEqual((app / "Contents" / "stamp").read_text(), stamp, "the running bundle is untouched")
        self.assertFalse([call for call in self.calls() if call.startswith(("open", "make"))][1:],
                         "after the first build nothing is built or opened")

    def test_the_real_process_list_finds_a_running_app_and_nothing_else(self):
        (self.bin / "ps").unlink()  # the real ps, so the check cannot match itself
        result = self.bbot("build", "ui")
        self.assertEqual(result.returncode, 0, "nothing is running: " + result.stderr)
        app = self.scaffold / ".bbot" / "ui" / "Brave Bot.app"
        helper = app / "Contents" / "Frameworks" / "Brave Bot Helper.app" / "Contents" / "MacOS" / "Brave Bot Helper"
        helper.parent.mkdir(parents=True)
        shutil.copy(os.environ["FAKE_SLEEPER"], helper)
        process = subprocess.Popen([str(helper)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        result = self.bbot("build", "ui")
        self.assertEqual(result.returncode, 1)
        self.assertIn("quit Brave Bot first", result.stderr)

    def test_a_failed_copy_leaves_no_partial_binary_and_the_record_untouched(self):
        self.assertEqual(self.bbot("build").returncode, 0)
        before = (self.scaffold / ".bbot" / "build-info").read_text()
        result = self.bbot("build", FAKE_CP_PARTIAL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.scaffold / ".bbot" / "bravebot.new").exists())
        self.assertEqual((self.scaffold / ".bbot" / "build-info").read_text(), before,
                         "the installed copy was never touched, so its record still describes it")

    def test_an_unreadable_process_list_refuses_the_desktop_install(self):
        result = self.bbot("build", "ui", FAKE_PS_FAILS="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot list running processes", result.stderr)
        self.assertFalse((self.scaffold / ".bbot" / "ui" / "Brave Bot.app").exists())

    def test_an_app_opened_while_the_bundle_is_copied_is_not_replaced(self):
        self.assertEqual(self.bbot("build", "ui").returncode, 0)
        app = self.scaffold / ".bbot" / "ui" / "Brave Bot.app"
        stamp = (app / "Contents" / "stamp").read_text()
        result = self.bbot("build", "ui", FAKE_DITTO_STARTS_APP=str(app))
        self.assertEqual(result.returncode, 1)
        self.assertEqual((app / "Contents" / "stamp").read_text(), stamp)

    def test_a_directory_inside_another_repository_is_not_the_checkout(self):
        (self.scaffold / "brave-scaffold.toml").write_text('schema_version = 1\nbravebot_path = "empty"\n')
        self.git(self.scaffold, "init", "-q")
        (self.scaffold / "empty").mkdir()
        result = self.bbot("build", cwd=self.scaffold)
        self.assertEqual(result.returncode, 1)
        self.assertIn("set bravebot_path", result.stderr)

    def test_an_app_opened_during_the_build_is_not_replaced(self):
        self.assertEqual(self.bbot("build", "ui").returncode, 0)
        app = self.scaffold / ".bbot" / "ui" / "Brave Bot.app"
        stamp = (app / "Contents" / "stamp").read_text()
        result = self.bbot("br", "ui", FAKE_MAKE_STARTS_APP=str(app))
        self.assertEqual(result.returncode, 1)
        self.assertIn("quit Brave Bot first", result.stderr)
        self.assertEqual((app / "Contents" / "stamp").read_text(), stamp)

    def test_status_says_when_the_checkout_is_not_a_repository(self):
        (self.temp / "plain").mkdir()
        (self.scaffold / "brave-scaffold.toml").write_text('schema_version = 1\nbravebot_path = "../plain"\n')
        result = self.bbot("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("set bravebot_path", result.stdout)

    def test_the_running_check_matches_a_path_with_special_characters_literally(self):
        moved = self.temp / "dev (old)"
        self.scaffold.rename(moved)
        self.scaffold = moved
        self.checkout = moved / "bravebot"
        self.assertEqual(self.bbot("build", "ui").returncode, 0)
        result = self.bbot("build", "ui", FAKE_APP_RUNNING=str(moved / ".bbot" / "ui" / "Brave Bot.app"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("quit Brave Bot first", result.stderr)

    def test_an_app_left_aside_is_still_found_by_ui_and_status(self):
        self.assertEqual(self.bbot("build", "ui").returncode, 0)
        ui = self.scaffold / ".bbot" / "ui"
        (ui / "Brave Bot.app").rename(ui / "Brave Bot.app.old")
        self.assertIn("[ui]\n", self.bbot("status").stdout)
        result = self.bbot("ui")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((ui / "Brave Bot.app").is_dir())
        self.assertEqual(self.calls()[-1], "open %s" % (ui / "Brave Bot.app"))

    def test_a_checkout_that_is_not_a_repository_points_at_the_setting(self):
        (self.temp / "plain").mkdir()
        (self.scaffold / "brave-scaffold.toml").write_text('schema_version = 1\nbravebot_path = "../plain"\n')
        result = self.bbot("build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("set bravebot_path", result.stderr)

    def test_leftovers_from_an_interrupted_run_are_removed(self):
        logs = self.scaffold / ".bbot" / "logs"
        logs.mkdir(parents=True)
        (logs / "old.log.cargo-report").write_text("{}\n")
        (logs / "old.log.status").write_text("0\n")
        self.assertEqual(self.bbot("build").returncode, 0)
        self.assertEqual(sorted(path.suffix for path in logs.iterdir()), [".log"])

    def test_a_failed_cargo_build_leaves_only_its_log(self):
        result = self.bbot("build", FAKE_CARGO_EXIT="101")
        self.assertEqual(result.returncode, 101)
        self.assertEqual([path.suffix for path in (self.scaffold / ".bbot" / "logs").iterdir()], [".log"])

    def test_running_inside_a_worktree_builds_that_worktree(self):
        worktree = self.checkout / ".worktrees" / "here"
        self.git(self.checkout, "worktree", "add", "-q", str(worktree), "-b", "here")
        (worktree / "sub").mkdir()
        result = self.bbot("build", cwd=worktree / "sub")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source=%s\n" % worktree, (self.scaffold / ".bbot" / "build-info").read_text())
        result = self.bbot("build", cwd=worktree, BBOT_SOURCE=str(self.checkout))
        self.assertIn("source=%s\n" % self.checkout, (self.scaffold / ".bbot" / "build-info").read_text(),
                      "an explicit selection wins")
        self.assertEqual(self.bbot("build", cwd=self.make_repo(self.temp / "unrelated")).returncode, 0)
        self.assertIn("source=%s\n" % self.checkout, (self.scaffold / ".bbot" / "build-info").read_text(),
                      "another repository falls back to the configured checkout")

    def test_the_desktop_app_is_built_installed_and_opened(self):
        result = self.bbot("br", "ui")
        self.assertEqual(result.returncode, 0, result.stderr)
        app = self.scaffold / ".bbot" / "ui" / "Brave Bot.app"
        self.assertTrue((app / "Contents").is_dir())
        self.assertIn("make -C %s app-bundle credentials=loaded" % self.checkout, self.calls())
        self.assertEqual(self.calls()[-1], "open %s" % app)

    def test_the_configured_checkout_is_used_relative_to_the_configuration(self):
        elsewhere = self.make_repo(self.temp / "repos" / "bravebot")
        (elsewhere / ".envrc").write_text("export SECRET=example\n")
        (self.scaffold / "brave-scaffold.toml").write_text('schema_version = 1\nbravebot_path = "../repos/bravebot"\n')
        result = self.bbot("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source=%s" % elsewhere,
                      (self.scaffold / ".bbot" / "build-info").read_text())
        (self.scaffold / "brave-scaffold.toml").write_text("schema_version = 1\nbravebot_path = [\n")
        result = self.bbot("status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bbot: cannot read", result.stderr)

    def test_builds_stop_without_direnv_an_envrc_or_an_approved_environment(self):
        for fake, expected in (("blocked", "not approved"), ("blocked-zero", "not approved")):
            with self.subTest(fake=fake):
                result = self.bbot("build", FAKE_DIRENV=fake)
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)
                self.assertIn("direnv allow", result.stderr)
        result = self.bbot("build", FAKE_DIRENV="fails")
        self.assertEqual(result.returncode, 1)
        self.assertIn("loading the environment", result.stderr)
        self.assertNotIn("direnv allow", result.stderr, "an approved .envrc that fails needs no approval")
        (self.bin / "direnv").unlink()
        result = self.bbot("build")
        self.assertEqual(result.returncode, 3)
        self.assertIn("direnv is required", result.stderr)
        (self.bin / "direnv").write_text(FAKE_DIRENV)
        (self.bin / "direnv").chmod(0o755)
        (self.checkout / ".envrc").unlink()
        result = self.bbot("build")
        self.assertEqual(result.returncode, 1)
        self.assertIn(".envrc is missing", result.stderr)
        self.assertFalse([call for call in self.calls() if call.startswith("cargo")], "nothing was built")
        self.assertFalse((self.scaffold / ".bbot" / "bravebot").exists(), "nothing was installed")
        self.assertTrue(all("refused: " in log.read_text() for log in self.logs()), "each refusal is logged")

    def test_a_path_containing_blocked_is_not_an_unapproved_environment(self):
        result = self.bbot("build", FAKE_DIRENV="unblocked-path")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_accepts_worktrees_of_the_checkout_only(self):
        worktrees = self.checkout / ".worktrees"
        self.git(self.checkout, "worktree", "add", "-q", str(worktrees / "feature"), "-b", "feature")
        result = self.bbot("--source", "feature", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source=%s" % (worktrees / "feature"), (self.scaffold / ".bbot" / "build-info").read_text())
        other = self.make_repo(self.temp / "other")
        result = self.bbot("--source", str(other), "build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a worktree of", result.stderr)
        result = self.bbot("--source", ".", "build", cwd=worktrees / "feature")
        self.assertEqual(result.returncode, 0, "'.' is a path, not a worktree name: " + result.stderr)
        self.assertIn("source=%s\n" % (worktrees / "feature"), (self.scaffold / ".bbot" / "build-info").read_text())
        status = self.bbot("--source", str(other), "status")
        self.assertIn("not a worktree of", status.stdout)
        result = self.bbot("--source", "missing", "build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no worktree 'missing'", result.stderr)
        (self.checkout / "ui").mkdir()
        result = self.bbot("--source", str(self.checkout / "ui"), "build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("top level", result.stderr)
        result = self.bbot("build", BBOT_SOURCE="feature")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source=%s" % (worktrees / "feature"), (self.scaffold / ".bbot" / "build-info").read_text())
        relative = os.path.relpath(worktrees / "feature", self.temp)
        result = self.bbot("build", BBOT_SOURCE=relative)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source=%s\n" % (worktrees / "feature"), (self.scaffold / ".bbot" / "build-info").read_text(),
                      "a relative BBOT_SOURCE is recorded as an absolute path")

    def test_worktrees_inside_the_checkout_do_not_make_it_dirty(self):
        self.git(self.checkout, "worktree", "add", "-q", str(self.checkout / ".worktrees" / "side"), "-b", "side")
        self.assertEqual(self.bbot("build").returncode, 0)
        self.assertIn("state=clean", (self.scaffold / ".bbot" / "build-info").read_text())
        self.assertNotIn("uncommitted", self.bbot("run").stderr)

    def test_running_warns_when_the_checkout_moved_on_or_has_uncommitted_changes(self):
        self.assertEqual(self.bbot("build").returncode, 0)
        (self.checkout / "notes.txt").write_text("local\n")
        result = self.bbot("run")
        self.assertIn("uncommitted changes", result.stderr)
        self.git(self.checkout, "add", "notes.txt")
        self.git(self.checkout, "commit", "-q", "-m", "notes")
        result = self.bbot("run")
        self.assertIn("does not match HEAD", result.stderr)
        self.assertEqual(result.stdout.strip(), "bravebot run", "it still runs the installed copy")

    def test_running_warns_about_a_build_from_uncommitted_changes_or_a_deleted_source(self):
        (self.checkout / "notes.txt").write_text("local\n")
        self.assertEqual(self.bbot("build").returncode, 0)
        (self.checkout / "notes.txt").unlink()
        self.assertIn("made from uncommitted changes", self.bbot("run").stderr, "the tree is clean again")
        worktree = self.checkout / ".worktrees" / "gone"
        self.git(self.checkout, "worktree", "add", "-q", str(worktree), "-b", "gone")
        self.assertEqual(self.bbot("--source", "gone", "build").returncode, 0)
        self.git(self.checkout, "worktree", "remove", "--force", str(worktree))
        self.assertIn("no longer exists", self.bbot("run").stderr)

    def logs(self):
        return sorted((self.scaffold / ".bbot" / "logs").glob("*.log"))

    def test_each_build_is_logged_with_its_commands_directories_and_output(self):
        result = self.bbot("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        (log,) = self.logs()
        self.assertTrue(log.name.endswith("-build.log"))
        self.assertIn("Log: %s" % log, result.stderr)
        self.assertIn("Compiling bravebot (fake)", result.stderr, "child output still streams to the console")
        self.assertIn("$ (in %s) direnv exec %s env BRAVEBOT_ALLOW_UNCONFIGURED_BUILD=0 cargo build" % (self.checkout, self.checkout), result.stderr,
                      "the console shows the primary command")
        text = log.read_text()
        self.assertTrue(text.startswith("bbot build\n"))
        self.assertIn("source=%s" % self.checkout, text)
        self.assertIn("$ (in %s) direnv exec %s env BRAVEBOT_ALLOW_UNCONFIGURED_BUILD=0 cargo build" % (self.checkout, self.checkout), text)
        self.assertIn("Compiling bravebot (fake)", text)
        self.assertIn("exit=0", text)
        self.assertNotIn('"executable"', text, "cargo's artifact report stays out of the log")
        self.assertNotIn("SECRET", text, "the log records no environment values")

    def test_a_failing_child_is_logged_and_its_status_returned(self):
        result = self.bbot("make", "test", FAKE_MAKE_EXIT="2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("make output for test", result.stderr)
        (log,) = self.logs()
        self.assertIn("$ (in %s) direnv exec %s make -C %s test" % (self.checkout, self.checkout, self.checkout),
                      log.read_text())
        self.assertIn("exit=2", log.read_text())

    def test_only_the_newest_logs_are_kept(self):
        logs = self.scaffold / ".bbot" / "logs"
        logs.mkdir(parents=True)
        for day in range(1, 26):
            (logs / ("200001%02dT000000Z-1-build.log" % day)).write_text("old\n")
        (logs / "notes.txt").write_text("mine\n")
        self.assertEqual(self.bbot("cal").returncode, 0)
        kept = [path.name for path in self.logs()]
        self.assertEqual(len(kept), 20)
        self.assertTrue(kept[-1].endswith("-cal.log"))
        self.assertNotIn("20000101T000000Z-1-build.log", kept)
        self.assertTrue((logs / "notes.txt").exists(), "only bbot's own logs are removed")

    def test_usage_errors_explain_themselves(self):
        for args in (["build", "extra"], ["build", "ui", "extra"], ["build", "nonsense"], ["ui", "extra"],
                     ["cal", "extra"], ["--source"], ["br", "ui", "extra"], ["--source", "x", "run"],
                     ["--source", "x", "ui"]):
            with self.subTest(args=args):
                result = self.bbot(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage: bbot", result.stderr)
        result = self.bbot("hello")
        self.assertEqual(result.returncode, 1)
        self.assertIn("run 'bbot build'", result.stderr)


if __name__ == "__main__":
    unittest.main()
