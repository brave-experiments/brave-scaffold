# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""A local support repository and a fake adb for Android tests. Nothing here uses the network."""

import hashlib
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
src_root=../src
cp patches/marker "$src_root/SUPPORT_PATCHED"
mkdir -p "$src_root/base" && echo "support patched" > "$src_root/base/support_target.cc"
if [ -f "$src_root/base/BUILD.gn" ] && ! grep -q "support edit" "$src_root/base/BUILD.gn"; then
  echo "support edit" >> "$src_root/base/BUILD.gn"
fi
"""

REALISTIC_APPLY_SCRIPT = """#!/bin/bash
cd "$(dirname "$0")"
verify=false
[ "${1:-}" = "-v" ] && verify=true
src_root=$(cd ../src && pwd -P)
handle_patch() {
  local repo=$2 patch=$3 file
  if $verify; then git -C "$repo" apply --reverse --check "$patch" >/dev/null 2>&1; return $?; fi
  while read -r file; do
    git -C "$repo" checkout -- "$file" || return 1
  done < <(sed -n 's|^--- [^/]*/||p' "$patch" | grep -v '^dev/null' | sort -u)
  git -C "$repo" apply "$patch"
}
android_host_assert() {
  local build_config="$src_root/build/config/BUILDCONFIG.gn"
  local assertion='assert(host_os == "linux")'
  grep -Fq "$assertion" "$build_config" || return 0
  $verify && return 1
  sed -i '' "/$assertion/d" "$build_config"
}
failures=0
handle_patch "fork" "$src_root" "$PWD/patches/build-config-fork.patch" || failures=1
handle_patch "a" "$src_root" "$PWD/patches/support-a-prefix.patch" || failures=1
android_host_assert || failures=1
handle_patch "nested" "$src_root/v8" "$PWD/patches/v8-nested.patch" || failures=1
exit $failures
"""

REALISTIC_PATCHES = {
    "build-config-fork.patch": (
        "diff --git forkSrcPrefix/build/config/support_fork.gni forkDstPrefix/build/config/support_fork.gni\n"
        "--- forkSrcPrefix/build/config/support_fork.gni\n+++ forkDstPrefix/build/config/support_fork.gni\n"
        "@@ -1 +1 @@\n-original fork\n+patched fork\n"),
    "support-a-prefix.patch": (
        "diff --git a/support/target_a.cc b/support/target_a.cc\n"
        "--- a/support/target_a.cc\n+++ b/support/target_a.cc\n"
        "@@ -1,5 +1,5 @@\n line1\n-line2\n+patched line2\n line3\n line4\n line5\n"),
    "v8-nested.patch": (
        "diff --git forkSrcPrefix/gni/snapshot.gni forkDstPrefix/gni/snapshot.gni\n"
        "--- forkSrcPrefix/gni/snapshot.gni\n+++ forkDstPrefix/gni/snapshot.gni\n"
        "@@ -1 +1 @@\n-v8 original\n+v8 patched\n"),
}


def script_contracts():
    """Complete write inventories for the fixed scripts this fixture supplies."""
    resources = [["third_party/jdk", "res/jdk/current"]]
    extra_copy = COPY_SCRIPT + 'patch_dependency "Extra" "third_party/extra" "" "res/extra/current" ""\n'
    entries = [
        (COPY_SCRIPT, "copyMacRes.sh", [], [], resources),
        (extra_copy, "copyMacRes.sh", [], [], resources + [["third_party/extra", "res/extra/current"]]),
        (APPLY_SCRIPT, "applyPatches.sh", [["", "support.patch"]],
         ["SUPPORT_PATCHED", "base/BUILD.gn", "base/support_target.cc"], []),
        (REALISTIC_APPLY_SCRIPT, "applyPatches.sh", [["", "build-config-fork.patch"],
         ["", "support-a-prefix.patch"], ["v8", "v8-nested.patch"]], ["build/config/BUILDCONFIG.gn"], []),
    ]
    return {hashlib.sha256(text.encode()).hexdigest():
            {"script": name, "patches": patches, "direct": direct, "resources": copied}
            for text, name, patches, direct, copied in entries}

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
elif "force-stop" in args:
    sys.exit(int(os.environ.get("FAKE_ADB_FORCE_STOP_FAIL", "0")))
elif "pidof" in args:
    if os.environ.get("FAKE_ADB_NO_PID"):
        sys.exit(1)
    print("4321")
"""


FAKE_AAPT2 = """#!%(python)s
import os, sys
if os.environ.get("FAKE_AAPT2_FAIL"):
    sys.exit(1)
print(os.environ.get("FAKE_APK_PACKAGE", "com.brave.browser_default"))
"""


def install_fake_aapt2(src):
    write_executable(Path(src) / "third_party" / "android_build_tools" / "aapt2" / "cipd" / "aapt2", FAKE_AAPT2)


def make_support_repo(root: Path, versions: dict, realistic: bool = False, lfs: bool = False) -> Path:
    """A support repository with one tagged commit per {tag: supported Chromium major}. Returns its path.

    A realistic repository applies real patches with upstream-style headers, including one in a nested
    repository, and edits a build file directly.
    """
    repo = Path(root) / "support-source"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    if lfs:
        subprocess.run(["git", "-C", str(repo), "lfs", "install", "--local"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "lfs", "track", "res/jdk/current/large.bin"], check=True,
                       capture_output=True)
    for tag, major in versions.items():
        (repo / "SUPPORTS_CHROMIUM").write_text(str(major))
        write_executable(repo / "copyMacRes.sh", COPY_SCRIPT)
        write_executable(repo / "applyPatches.sh", REALISTIC_APPLY_SCRIPT if realistic else APPLY_SCRIPT)
        (repo / "patches").mkdir(exist_ok=True)
        if realistic:
            for name, text in REALISTIC_PATCHES.items():
                (repo / "patches" / name).write_text(text)
        else:
            (repo / "patches" / "marker").write_text("patched-for-%s\n" % tag)
            (repo / "patches" / "support.patch").write_text("diff --git a/base/support_target.cc b/base/support_target.cc\n")
        (repo / "res" / "jdk" / "current").mkdir(parents=True, exist_ok=True)
        (repo / "res" / "jdk" / "current" / "release").write_text("JAVA_VERSION=25 %s\n" % tag)
        if lfs:
            (repo / "res" / "jdk" / "current" / "large.bin").write_bytes(b"large-file-content-" * 100)
        subprocess.run([*GIT, "-C", str(repo), "add", "-A"], check=True)
        subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "-m", tag], check=True)
        subprocess.run([*GIT, "-C", str(repo), "tag", tag], check=True)
    subprocess.run([*GIT, "-C", str(repo), "branch", "android-testing-prototype"], check=True)
    return repo


def install_fake_adb(sandbox):
    write_executable(sandbox.bin / "adb", FAKE_ADB)
