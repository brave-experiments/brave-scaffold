# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""A local support repository and a fake adb for Android tests. Nothing here uses the network."""

import subprocess
from pathlib import Path

from tests.support import write_executable

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]

COPY_SCRIPT = """#!/bin/bash
cd "$(dirname "$0")"
major=$(grep '^MAJOR=' ../src/chrome/VERSION | cut -d= -f2)
supported=$(cat SUPPORTS_CHROMIUM)
if [ "$major" != "$supported" ]; then
  echo "❌ Error: this revision supports Chromium $supported but the checkout is $major." >&2
  exit 1
fi
if [ "$1" = "-v" ]; then exit 0; fi
patch_dependency() { mkdir -p "../src/$2" && cp -R "$4" "../src/$2/"; }
patch_dependency "JDK" "third_party/jdk" "Version: 25" "res/jdk/current" "third_party/jdk/README.chromium"
"""

APPLY_SCRIPT = """#!/bin/bash
cd "$(dirname "$0")"
if [ "$1" = "-v" ]; then
  cmp -s patches/marker ../src/SUPPORT_PATCHED
  exit $?
fi
cp patches/marker ../src/SUPPORT_PATCHED
mkdir -p ../src/base && echo "support patched" > ../src/base/support_target.cc
if [ -f ../src/base/BUILD.gn ] && ! grep -q "support edit" ../src/base/BUILD.gn; then
  echo "support edit" >> ../src/base/BUILD.gn
fi
"""

FAKE_ADB = """#!%(python)s
import json, os, sys
args = sys.argv[1:]
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "a") as stream:
        stream.write(json.dumps({"tool": "adb", "argv": args}) + "\\n")
if args == ["devices"]:
    print("List of devices attached")
    for entry in filter(None, os.environ.get("FAKE_ADB_DEVICES", "").split(";")):
        print(entry.replace(",", "\\t"))
elif "install" in args:
    print("Success" if not os.environ.get("FAKE_ADB_INSTALL_FAIL") else "Failure [INSTALL_FAILED]")
    sys.exit(int(os.environ.get("FAKE_ADB_INSTALL_FAIL", "0")))
elif "pidof" in args:
    if os.environ.get("FAKE_ADB_NO_PID"):
        sys.exit(1)
    print("4321")
"""


def make_support_repo(root: Path, versions: dict) -> Path:
    """A support repository with one tagged commit per {tag: supported Chromium major}. Returns its path."""
    repo = Path(root) / "support-source"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for tag, major in versions.items():
        (repo / "SUPPORTS_CHROMIUM").write_text(str(major))
        write_executable(repo / "copyMacRes.sh", COPY_SCRIPT)
        write_executable(repo / "applyPatches.sh", APPLY_SCRIPT)
        (repo / "patches").mkdir(exist_ok=True)
        (repo / "patches" / "marker").write_text("patched-for-%s\n" % tag)
        (repo / "patches" / "support.patch").write_text("diff --git a/base/support_target.cc b/base/support_target.cc\n")
        (repo / "res" / "jdk" / "current").mkdir(parents=True, exist_ok=True)
        (repo / "res" / "jdk" / "current" / "release").write_text("JAVA_VERSION=25 %s\n" % tag)
        subprocess.run([*GIT, "-C", str(repo), "add", "-A"], check=True)
        subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "-m", tag], check=True)
        subprocess.run([*GIT, "-C", str(repo), "tag", tag], check=True)
    return repo


def install_fake_adb(sandbox):
    write_executable(sandbox.bin / "adb", FAKE_ADB)
