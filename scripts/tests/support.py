# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Disposable checkouts, fake tools, and an isolated direnv for behavioral tests."""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "src"))
os.environ["BCORE_NOTIFY_BACKEND"] = "none"

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
if len(sys.argv) > 1 and sys.argv[1].endswith(("tarball_installer.py", "install_extra_deps.py")):
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
import glob
workspace = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
for stale in glob.glob(os.path.join(workspace, "STALE*")):
    os.unlink(stale)
"""
APP_HELPERS = """
import plistlib, shutil, stat
def make_app(directory, name="Brave Browser Development", bundle_id=None, program=None):
    bundle_id = bundle_id or os.environ.get("FAKE_BUNDLE_ID", "com.brave.ScaffoldTest")
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
    marker = lambda name: os.path.exists(os.path.join(str(root), name))
    if marker("STALE"):
        return False
    return not (marker("STALE-pnpm") and "node_modules" in path or marker("STALE-node") and "node-mac" in path)
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
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
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


GCLIENT_ENTRIES = """entries = {
  'src': 'https://example.invalid/chromium/src.git',
  'src/brave': 'https://example.invalid/brave-core.git',
  'src/base/tracing/test/data:test_data/example.gz-1': 'gs://example/test_data/example.gz-1',
}
"""
BRAVE_GCLIENT_ENTRIES = """entries = {
  '.': 'https://example.invalid/brave-core.git',
}
"""


class Sandbox:
    """A temp directory holding a scaffold configuration, checkouts, and fake PATH tools."""

    def __init__(self):
        self.scripts = SCRIPTS
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
        self.bundle_id = "com.brave.ScaffoldTest." + secrets.token_hex(4)
        write_executable(self.bin / "xcode-select", "#!/bin/sh\necho /fake/Xcode/Developer\n")
        write_executable(self.bin / "xcrun", "#!/bin/sh\necho 15.0\n")
        write_executable(self.bin / "osascript", FAKE_OSASCRIPT)
        write_executable(self.bin / "open", FAKE_OPEN)

    def start_app(self, directory, bundle_id=None, name="Brave Browser Development",
                  program=None, arguments=("300",)):
        """A running instance of a fake application bundle; returns (bundle path, Popen)."""
        app = make_app(str(directory), name=name, bundle_id=bundle_id or self.bundle_id, program=program)
        process = subprocess.Popen([os.path.join(app, "Contents", "MacOS", name), *arguments],
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        with open(self.root / "app-pids", "a") as stream:
            stream.write(" %d" % process.pid)
        return app, process

    def stray_pids(self):
        """Processes whose command line mentions this sandbox: fake apps, fake package commands, helpers."""
        listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        return [int(line.split(None, 1)[0]) for line in listing.splitlines()
                if str(self.root) in line and int(line.split(None, 1)[0]) != os.getpid()]

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        for pid in self.stray_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self, **extra):
        env = {"PATH": "%s:/usr/bin:/bin:/opt/homebrew/bin" % self.bin, "HOME": str(self.home),
               "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.root / "xdg"),
               "FAKE_BUNDLE_ID": self.bundle_id, "FAKE_RECORD": str(self.record), "FAKE_APP_PIDS": str(self.root / "app-pids"), "FAKE_SLEEPER": os.environ["FAKE_SLEEPER"],
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
        workspace = src.parent
        (workspace / ".gclient_entries").write_text(GCLIENT_ENTRIES)
        (core / ".brave_gclient_entries").write_text(BRAVE_GCLIENT_ENTRIES)
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

    def add_dependency(self, name, relative="v8", listed=True):
        """A separate Git repository inside Chromium's source root that gclient manages; returns its path."""
        core = self.checkouts[name]
        src = core.parent
        repo = src.joinpath(*relative.split("/"))
        repo.mkdir(parents=True)
        options = ["-c", "user.name=Test", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "test.cc").write_text("upstream\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), *options, "commit", "-q", "-m", "dependency"], check=True)
        exclude = src / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "/%s/\n" % relative.split("/")[0])
        if listed:
            entries = src.parent / ".gclient_entries"
            entries.write_text(entries.read_text().replace(
                "}\n", "  'src/%s': 'https://example.invalid/%s.git@abc',\n}\n" % (relative, relative.replace("/", "-"))))
        return repo

    def commit_all(self, name):
        """Commit Core and Chromium contents so Git-based inputs are clean."""
        core = self.checkouts[name]
        for repo in (core, core.parent):
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                            "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "fixture"],
                           check=True, capture_output=True)

    def configure_rbe(self, name, **env_overrides):
        """Local remote-build configuration that satisfies the readiness checks (no service is contacted)."""
        core = self.checkouts[name]
        write_executable(self.bin / "openssl", "#!/bin/sh\nexit ${FAKE_OPENSSL_EXIT:-0}\n")
        service = "https://user:pw@rbe.example:443"
        certs = self.root / "rbe-files"
        certs.mkdir(exist_ok=True)
        (certs / "client.crt").write_text("cert")
        (certs / "client.key").write_text("key")
        cache = certs / "siso-cache"
        cache.mkdir(exist_ok=True)
        values = {"rbe_service": service, "use_remoteexec": "true", "rbe_tls_client_auth_cert": str(certs / "client.crt"),
                  "rbe_tls_client_auth_key": str(certs / "client.key"), "siso_cache_dir": str(cache)}
        values.update(env_overrides)
        (core / ".env").write_text("".join("%s=%s\n" % item for item in values.items() if item[1] is not None))
        siso = core.parent / "build" / "config" / "siso"
        siso.mkdir(parents=True, exist_ok=True)
        (core.parent.parent / ".gclient").write_text('custom_vars = {"reapi_address": "%s"}\n' % service)
        (siso / ".sisorc").write_text('reapi_keep_exec_stream googlechrome -local_cache_enable -cache_dir "%s"\n' % cache)
        (siso / ".sisoenv").write_text("REAPI=%s\n" % service)

    def hook(self, code):
        """Python run inside the fake package command; APP_HELPERS is available."""
        path = self.root / "hook.py"
        path.write_text(APP_HELPERS + "\n" + code)
        return str(path)

    def mark_stale(self, name, only=None):
        """Make the fake payload metadata report stale: everything, or only the "node" or "pnpm" entry."""
        (self.checkouts[name].parent.parent / ("STALE-" + only if only else "STALE")).write_text("")

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

    def bcore(self, *args, cwd=None, env=None, tool="bcore"):
        return subprocess.run([str(self.scripts / tool), *args], cwd=str(cwd or self.root),
                              env=env or self.env(), capture_output=True, text=True)

    def install_script_contracts(self, contracts):
        """Give a private tooling copy reviewed manifests for the fixture scripts.

        Production tools have no test override or support-owned approval path.
        """
        self.scripts = self.root / "tooling" / "scripts"
        shutil.copytree(SCRIPTS / "src", self.scripts / "src", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("bcore", "bpm", "git-sign-with-1password"):
            shutil.copy2(SCRIPTS / name, self.scripts / name)
        (self.scripts / ".venv").symlink_to(SCRIPTS / ".venv", target_is_directory=True)
        manifest = self.scripts / "src" / "scaffold" / "brave" / "support_script_contracts.json"
        known = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**known, **contracts}))

    def bcore_json(self, *args, **kwargs):
        result = self.bcore("--json", *args, **kwargs)
        document = json.loads(result.stdout)
        return result, document

    def approve(self, name):
        directory = self.root / "config" / "environments" / name
        subprocess.run(["direnv", "allow", str(directory)], env=self.env(), check=True, capture_output=True)

    def prepare_environment(self, name="main", approve=True):
        """Register the checkout, generate its environment, and (in this disposable
        sandbox only) approve it."""
        self.register(name)
        result = self.bcore("env", "init", "--checkout", name, "--config", str(self.config))
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
