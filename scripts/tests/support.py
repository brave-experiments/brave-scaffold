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
EXTRA_DEPS = """import os
def check_extra_deps_installed(root, path):
    return not os.path.exists(os.path.join(str(root), "STALE"))
"""


def write_executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text % {"python": PYTHON} if "%(python)s" in text else text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


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

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self, **extra):
        env = {"PATH": "%s:/usr/bin:/bin:/opt/homebrew/bin" % self.bin, "HOME": str(self.home),
               "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.root / "xdg"),
               "FAKE_RECORD": str(self.record), "LANG": "en_US.UTF-8"}
        env.update(extra)
        return env

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def make_checkout(self, name="main", manager="pnpm", node_version="v24.20.0", declaration=True,
                      node_payload=True, manager_version="11.25.0", payload_metadata=True):
        outer = self.root / ("brave-" + name)
        core = outer / "_bad_scm" / "main" / "src" / "brave"
        src = core.parent
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
