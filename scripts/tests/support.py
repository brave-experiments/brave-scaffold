# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Disposable checkouts, fake tools, and an isolated direnv for behavioral tests."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "src"))

PYTHON = sys.executable
FAKE_NODE = """#!%(python)s
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:] == ["--version"]:
    print(open(os.path.join(here, "version")).read().strip())
    sys.exit(0)
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "a") as stream:
        stream.write(json.dumps({"tool": "node", "argv": sys.argv[1:], "cwd": os.getcwd(),
                                 "path": os.environ.get("PATH", ""),
                                 "launcher": os.environ.get("BRAVE_LAUNCHER_CHECKOUT_DIR"),
                                 "pid": os.getpid()}) + "\\n")
if os.environ.get("FAKE_SLEEP"):
    import time
    time.sleep(float(os.environ["FAKE_SLEEP"]))
namespace = {"argv": sys.argv[1:], "os": os, "sys": sys, "exit_code": int(os.environ.get("FAKE_EXIT", "0"))}
if os.environ.get("FAKE_HOOK"):
    exec(compile(open(os.environ["FAKE_HOOK"]).read(), os.environ["FAKE_HOOK"], "exec"), namespace)
    sys.exit(int(namespace["exit_code"]))
if os.environ.get("FAKE_STDOUT"):
    print(os.environ["FAKE_STDOUT"])
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""
FAKE_VPYTHON = """#!%(python)s
import json, os, sys
if len(sys.argv) > 1 and sys.argv[1].endswith("tarball_installer.py"):
    os.execv(sys.executable, [sys.executable] + sys.argv[1:])
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "a") as stream:
        stream.write(json.dumps({"tool": "vpython3", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""
FAKE_INSTALLER = """#!%(python)s
import json, os, sys
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "a") as stream:
        stream.write(json.dumps({"tool": "installer", "argv": sys.argv[1:]}) + "\\n")
stale = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "STALE")
if os.path.exists(stale):
    os.unlink(stale)
"""
APP_HELPERS = """
import plistlib, shutil, stat
def make_app(directory, name="Brave Browser Development", bundle_id="com.brave.ScaffoldTest", program=None):
    program = program or os.environ["FAKE_SLEEPER"]
    app = os.path.join(directory, name + ".app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"), exist_ok=True)
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as stream:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": name, "CFBundleName": name}, stream)
    binary = os.path.join(app, "Contents", "MacOS", name)
    shutil.copy(program, binary)
    os.chmod(binary, os.stat(binary).st_mode | stat.S_IXUSR)
    return app
"""
FAKE_OSASCRIPT = """#!%(python)s
import os, plistlib, re, signal, subprocess, sys
if os.environ.get("FAKE_QUIT", "graceful") == "refuse":
    sys.exit(1)
match = re.search(r'application id "([^"]+)"', " ".join(sys.argv[1:]))
wanted = match.group(1) if match else None
pids = os.environ.get("FAKE_APP_PIDS", "/nonexistent")
for pid in (open(pids).read().split() if os.path.exists(pids) else []):
    command = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
    found = re.match(r"(.*?[.]app)/Contents/MacOS/", command.strip())
    if not found:
        continue
    try:
        info = plistlib.loads(open(os.path.join(found.group(1), "Contents", "Info.plist"), "rb").read())
    except OSError:
        continue
    if info.get("CFBundleIdentifier") == wanted:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
"""
FAKE_OPEN = """#!%(python)s
import json, os, plistlib, subprocess, sys
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "a") as stream:
        stream.write(json.dumps({"tool": "open", "path": sys.argv[1]}) + "\\n")
if os.environ.get("FAKE_OPEN_EXIT"):
    sys.exit(int(os.environ["FAKE_OPEN_EXIT"]))
info = plistlib.loads(open(os.path.join(sys.argv[1], "Contents", "Info.plist"), "rb").read())
binary = os.path.join(sys.argv[1], "Contents", "MacOS", info["CFBundleExecutable"])
process = subprocess.Popen([binary, "300"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pids = os.environ.get("FAKE_APP_PIDS")
if pids:
    with open(pids, "a") as stream:
        stream.write(" %%d" %% process.pid)
"""
EXTRA_DEPS = """import os
def check_extra_deps_installed(root, path):
    return not os.path.exists(os.path.join(str(root), "STALE"))
"""


def write_executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text % {"python": PYTHON} if "%(python)s" in text else text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


SLEEPER_SOURCE = """
#include <signal.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc > 1 && argv[1][0] == 'i') signal(SIGTERM, SIG_IGN);
  for (;;) sleep(1);
}
"""


def _build_sleeper():
    """A local executable to stand in for a browser process. Copies of system binaries are killed."""
    directory = tempfile.mkdtemp(prefix="scaffold-sleeper-")
    source = os.path.join(directory, "sleeper.c")
    with open(source, "w") as stream:
        stream.write(SLEEPER_SOURCE)
    binary = os.path.join(directory, "sleeper")
    subprocess.run(["cc", "-o", binary, source], check=True, capture_output=True)
    return binary


try:
    os.environ["FAKE_SLEEPER"] = _build_sleeper()
except (OSError, subprocess.CalledProcessError):
    os.environ["FAKE_SLEEPER"] = "/bin/sleep"
exec(APP_HELPERS)


class Sandbox:
    """A temp directory holding a scaffold configuration, checkouts, and fake PATH tools."""

    def __init__(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="scaffold-test-")))
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.bin = self.root / "bin"
        self.record = self.root / "record.jsonl"
        for directory in (self.home, self.data, self.bin, self.root / "config"):
            directory.mkdir(parents=True)
        self.config = self.root / "config" / "brave-scaffold.toml"
        self.checkouts = {}
        self.processes = []
        write_executable(self.bin / "xcode-select", "#!/bin/sh\necho /fake/Xcode/Developer\n")
        write_executable(self.bin / "xcrun", "#!/bin/sh\necho 15.0\n")
        write_executable(self.bin / "osascript", FAKE_OSASCRIPT)
        write_executable(self.bin / "open", FAKE_OPEN)

    def start_app(self, directory, bundle_id="com.brave.ScaffoldTest", name="Brave Browser Development",
                  program=None, arguments=("300",)):
        """A running instance of a fake application bundle; returns (bundle path, Popen)."""
        app = make_app(str(directory), name=name, bundle_id=bundle_id, program=program)
        process = subprocess.Popen([os.path.join(app, "Contents", "MacOS", name), *arguments],
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        with open(self.root / "app-pids", "a") as stream:
            stream.write(" %d" % process.pid)
        return app, process

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self, **extra):
        env = {"PATH": "%s:/usr/bin:/bin:/opt/homebrew/bin" % self.bin, "HOME": str(self.home),
               "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.root / "xdg"),
               "FAKE_RECORD": str(self.record), "FAKE_APP_PIDS": str(self.root / "app-pids"), "FAKE_SLEEPER": os.environ["FAKE_SLEEPER"],
               "LANG": "en_US.UTF-8"}
        env.update(extra)
        return env

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def make_checkout(self, name="main", manager="pnpm", node_version="v24.20.0", declaration=True,
                      node_payload=True, manager_version="11.25.0", payload_metadata=True, git=False):
        outer = self.root / ("brave-" + name)
        core = outer / "_bad_scm" / "main" / "src" / "brave"
        src = core.parent
        core.mkdir(parents=True)
        if git:
            for repo in (core, src):
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (src / ".git" / "info").mkdir(exist_ok=True)
            (src / ".git" / "info" / "exclude").write_text("/brave/\n/out/\n")
        else:
            (core / ".git").mkdir(parents=True)
            (src / ".git").mkdir(parents=True)
        package = {"name": "brave-core"}
        if declaration:
            package["devEngines"] = {"runtime": {"name": "node", "version": ">=24.16.0 <25.0.0"},
                                     "packageManager": {"name": manager, "version": ">=11.11.0"}}
        (core / "package.json").write_text(json.dumps(package))
        (core / "script").mkdir()
        write_executable(core / "vendor" / "depot_tools" / "vpython3", FAKE_VPYTHON)
        node_dir = core / "third_party" / "node" / "node-mac-arm64"
        if node_payload:
            write_executable(node_dir / "bin" / "node", FAKE_NODE)
            (node_dir / "bin" / "version").write_text(node_version)
            (node_dir / "lib" / "node_modules" / "npm" / "bin").mkdir(parents=True)
            (node_dir / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js").write_text("")
            (node_dir / "lib" / "node_modules" / "npm" / "package.json").write_text('{"version": "10.9.0"}')
            pnpm = core / "third_party" / "node" / "node_modules" / "pnpm"
            (pnpm / "bin").mkdir(parents=True)
            (pnpm / "bin" / "pnpm.mjs").write_text("")
            (pnpm / "package.json").write_text(json.dumps({"version": manager_version}))
        if payload_metadata:
            (core / "tools" / "cr").mkdir(parents=True)
            (core / "tools" / "cr" / "extra_deps.py").write_text(EXTRA_DEPS)
        write_executable(core / "tools" / "cr" / "tarball_installer.py", FAKE_INSTALLER)
        self.checkouts[name] = core
        return core

    def add_patch(self, name, target, before="original\n", after="patched\n", patch_name=None, record=True):
        """A patch with metadata whose target file currently holds its patched content."""
        core = self.checkouts[name]
        src = core.parent
        (core / "patches").mkdir(exist_ok=True)
        patch_name = patch_name or target.replace("/", "-") + ".patch"
        patch = core / "patches" / patch_name
        patch.write_text("diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n-%s+%s" % (
            target, target, target, target, before, after))
        (src / target).parent.mkdir(parents=True, exist_ok=True)
        (src / target).write_text(after)
        if record:
            import hashlib
            info = {"schemaVersion": 1, "patchChecksum": hashlib.sha256(patch.read_bytes()).hexdigest(),
                    "appliesTo": [{"path": target, "checksum": hashlib.sha256((src / target).read_bytes()).hexdigest()}]}
            patch.with_suffix(".patchinfo").write_text(json.dumps(info))
        return patch

    def commit_all(self, name):
        """Commit Core and Chromium contents so Git-based inputs are clean."""
        core = self.checkouts[name]
        for repo in (core, core.parent):
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                            "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "fixture"],
                           check=True, capture_output=True)

    def hook(self, code):
        """Python run inside the fake package command; APP_HELPERS is available."""
        path = self.root / "hook.py"
        path.write_text(APP_HELPERS + "\n" + code)
        return str(path)

    def mark_stale(self, name):
        (self.checkouts[name].parent.parent / "STALE").write_text("")

    def write_config(self, entries, extra=""):
        """entries: list of (alias|None, core, direnv_dir|None)."""
        lines = ["schema_version = 1", ""]
        for alias, core, directory in entries:
            lines.append("[[checkouts]]")
            if alias:
                lines.append('alias = "%s"' % alias)
            lines.append('core = "%s"' % core)
            if directory:
                lines.append('direnv_dir = "%s"' % directory)
            lines.append("")
        self.config.write_text("\n".join(lines) + extra)

    def register(self, name="main", alias=True):
        core = self.checkouts[name]
        self.write_config([(name if alias else None, core, "environments/" + name)])

    def bdev(self, *args, cwd=None, env=None, tool="bdev"):
        return subprocess.run([str(SCRIPTS / tool), *args], cwd=str(cwd or self.root),
                              env=env or self.env(), capture_output=True, text=True)

    def bdev_json(self, *args, **kwargs):
        result = self.bdev("--json", *args, **kwargs)
        document = json.loads(result.stdout)
        return result, document

    def approve(self, name):
        directory = self.root / "config" / "environments" / name
        subprocess.run(["direnv", "allow", str(directory)], env=self.env(), check=True, capture_output=True)

    def prepare_environment(self, name="main", approve=True):
        """Register the checkout, generate its environment, and (in this disposable
        sandbox only) approve it."""
        self.register(name)
        result = self.bdev("env", "init", "--checkout", name, "--config", str(self.config))
        assert result.returncode == 0, result.stderr
        if approve:
            self.approve(name)


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)


def tree_snapshot(path):
    """Content and mode fingerprint of every file below a directory."""
    snapshot = {}
    for current, dirs, files in os.walk(path):
        for name in sorted(files + dirs):
            item = Path(current) / name
            if item.is_symlink():
                snapshot[str(item)] = ("link", os.readlink(item))
            elif item.is_file():
                snapshot[str(item)] = ("file", item.read_bytes(), item.stat().st_mode)
            else:
                snapshot[str(item)] = ("dir",)
    return snapshot
