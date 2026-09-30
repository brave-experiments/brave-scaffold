# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch preparation covers every repository Core patches and every file a patch can write."""

import hashlib
import json
import subprocess
import unittest

from tests.integration.test_build import SKIP, BuildTestCase

APPLY_LIKE_CORE = """
if "apply_patches" in argv:
    import glob, hashlib, json, re
    core = os.environ["BRAVE_CORE_DIR"]
    src = os.path.dirname(core)
    repositories = [""]
    cfg = os.path.join(core, "patches", ".repositories.cfg")
    if os.path.exists(cfg):
        repositories = [line.strip()[2:].strip("/") for line in open(cfg) if line.strip().startswith("//")]
    sha = lambda path: hashlib.sha256(open(path, "rb").read()).hexdigest()
    for relative in repositories:
        repo = os.path.join(src, relative)
        for patch in sorted(glob.glob(os.path.join(core, "patches", relative, "*.patch"))):
            info = patch[:-len(".patch")] + ".patchinfo"
            targets = re.findall(r"^diff --git a/(\\S+) b/", open(patch).read(), re.M)
            for target in targets:
                os.makedirs(os.path.dirname(os.path.join(repo, target)) or repo, exist_ok=True)
                open(os.path.join(repo, target), "w").write("applied by " + os.path.basename(patch) + "\\n")
                open(os.path.join(os.environ["FAKE_WROTE"]), "a").write(os.path.join(relative, target) + "\\n")
            json.dump({"schemaVersion": 1, "patchChecksum": sha(patch),
                       "appliesTo": [{"path": t, "checksum": sha(os.path.join(repo, t))} for t in targets]},
                      open(info, "w"))
    raise SystemExit(0)
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@unittest.skipIf(SKIP, "needs direnv on a macOS host")
class PatchScopeTests(BuildTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.core / "patches" / ".repositories.cfg"
        self.wrote = self.sandbox.root / "wrote.txt"
        self.hook = self.sandbox.hook(APPLY_LIKE_CORE + "\n" + self.hook_source_after_apply())
        self.repositories = ["//"]

    def hook_source_after_apply(self):
        from tests.integration.test_build import BUILD_HOOK
        return BUILD_HOOK

    def env(self, **extra):
        return super().env(FAKE_WROTE=str(self.wrote), **extra)

    def declare(self, *relatives):
        self.repositories += ["//" + relative for relative in relatives]
        self.cfg.write_text("\n".join(["# Chromium", *self.repositories]) + "\n")

    def add_repository(self, relative):
        """A separate Git repository under Chromium's source root that Core patches."""
        repo = self.src.joinpath(*relative.split("/"))
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        (repo / "keep.txt").write_text("upstream\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "dependency")
        exclude = self.src / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "/%s/\n" % relative.split("/")[0])
        self.declare(relative)
        return repo

    def add_nested_patch(self, relative, target, before="upstream\n", after="patched\n"):
        """A recorded patch in a nested repository; the target holds its patched content."""
        repo = self.src.joinpath(*relative.split("/"))
        (repo / target).parent.mkdir(parents=True, exist_ok=True)
        (repo / target).write_text(before)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "target")
        directory = self.core / "patches" / relative
        directory.mkdir(parents=True, exist_ok=True)
        patch = directory / (target.replace("/", "-") + ".patch")
        patch.write_text("diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n-%s+%s" % (target, target, target, target,
                                                                              before, after))
        (repo / target).write_text(after)
        patch.with_suffix(".patchinfo").write_text(json.dumps({
            "schemaVersion": 1, "patchChecksum": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "appliesTo": [{"path": target, "checksum": sha(after)}]}))
        return patch

    def change_patch(self, patch, *targets):
        """Rewrite a patch so it targets the given files; its metadata keeps describing the old content."""
        patch.write_text("".join("diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n-x\n+y\n" % ((target,) * 4)
                                 for target in targets))

    def prepare_build(self, args=("build",)):
        self.sandbox.record.unlink(missing_ok=True)
        return self.document(*args)

    def assert_stops(self, paths, args=("build",)):
        result, document = self.prepare_build(args)
        self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"), result.stderr)
        self.assertEqual({item["path"] for item in document["error"]["details"]["files"]}, set(paths))
        self.assertEqual(self.node_calls(), [], "neither apply_patches nor the build ran")
        return document

    def test_drift_covers_a_nested_repository(self):
        repo = self.add_repository("v8")
        self.add_nested_patch("v8", "BUILD.gn")
        self.sandbox.commit_all("main")
        result, document = self.document("drift")
        self.assertTrue(document["data"]["metadata_complete"], document["data"])
        self.assertEqual(document["data"]["patchinfo_files"], 2)
        self.assertEqual(document["data"]["drifted"], [])
        (repo / "BUILD.gn").write_text("my edit\n")
        result, document = self.document("drift", "--diff")
        self.assertEqual([item["path"] for item in document["data"]["drifted"]], ["v8/BUILD.gn"])
        self.assertTrue(document["data"]["drifted"][0]["numstat"].startswith("1\t1\t"), "diffed inside v8")

    def test_a_nested_or_deeper_repository_is_protected_before_an_upstream_patch_change(self):
        for relative, target in (("v8", "BUILD.gn"), ("third_party/devtools-frontend/src", "front/x.ts")):
            with self.subTest(repository=relative):
                repo = self.add_repository(relative)
                patch = self.add_nested_patch(relative, target)
                self.sandbox.commit_all("main")
                self.assertEqual(self.prepare_build()[0].returncode, 0)
                patch.write_text(patch.read_text() + "\n")
                (repo / target).write_text("my local experiment\n")
                self.assert_stops([relative + "/" + target])
                self.assertEqual((repo / target).read_text(), "my local experiment\n")
                (repo / target).write_text("patched\n")
                self.assertEqual(self.prepare_build()[0].returncode, 0, "recorded patch results are not local work")

    def gain_target(self, path):
        """Establish a receipt, then rewrite the root patch so it also targets `path` (its metadata does not)."""
        (self.src / "base" / "added.cc").write_text("upstream\n")
        self.sandbox.commit_all("main")
        self.assertEqual(self.prepare_build()[0].returncode, 0)
        self.change_patch(self.core / "patches" / "base-BUILD.gn.patch", "base/BUILD.gn", path)
        self.assertNotIn(path, (self.core / "patches" / "base-BUILD.gn.patchinfo").read_text())

    def test_a_patch_that_gains_a_target_stops_for_an_unstaged_edit_on_it(self):
        self.gain_target("base/added.cc")
        (self.src / "base" / "added.cc").write_text("my edit\n")
        self.assert_stops(["base/added.cc"])
        self.assertEqual((self.src / "base" / "added.cc").read_text(), "my edit\n")

    def test_a_patch_that_gains_a_target_stops_for_a_staged_edit_on_it(self):
        self.gain_target("base/added.cc")
        (self.src / "base" / "added.cc").write_text("my staged edit\n")
        git(self.src, "add", "base/added.cc")
        self.assert_stops(["base/added.cc"])
        self.assertEqual((self.src / "base" / "added.cc").read_text(), "my staged edit\n")

    def test_a_patch_that_gains_a_target_stops_for_a_deletion_of_it(self):
        self.gain_target("base/added.cc")
        (self.src / "base" / "added.cc").unlink()
        self.assert_stops(["base/added.cc"])
        self.assertFalse((self.src / "base" / "added.cc").exists())

    def test_a_patch_that_gains_an_untracked_target_stops(self):
        self.gain_target("base/new.cc")
        (self.src / "base" / "new.cc").write_text("mine\n")
        self.assert_stops(["base/new.cc"])
        self.assertEqual((self.src / "base" / "new.cc").read_text(), "mine\n")

    def test_untouched_new_targets_proceed_and_apply_writes_only_planned_files(self):
        self.add_repository("v8")
        self.add_nested_patch("v8", "BUILD.gn")
        self.gain_target("base/added.cc")
        self.wrote.unlink(missing_ok=True)
        self.sandbox.commit_all("main")
        result, document = self.prepare_build()
        self.assertEqual(result.returncode, 0, document.get("error"))
        self.assertIn("base/added.cc", self.wrote.read_text().split())

    def malformed(self, mutate):
        info = self.core / "patches" / "base-BUILD.gn.patchinfo"
        data = json.loads(info.read_text())
        mutate(data)
        info.write_text(json.dumps(data))
        self.sandbox.commit_all("main")
        (self.src / "base" / "BUILD.gn").write_text("my local experiment\n")

    def test_malformed_existing_metadata_never_lets_an_edited_target_be_overwritten(self):
        cases = {
            "missing checksum": lambda data: data["appliesTo"][0].pop("checksum"),
            "invalid path": lambda data: data["appliesTo"][0].update(path="../outside.cc"),
            "empty entry": lambda data: data["appliesTo"].append({}),
            "partial metadata": lambda data: data["appliesTo"].append({"path": "base/other.cc"}),
            "no appliesTo": lambda data: data.pop("appliesTo"),
            "wrong schema": lambda data: data.update(schemaVersion=99),
        }
        info = self.core / "patches" / "base-BUILD.gn.patchinfo"
        valid = info.read_text()
        for name, mutate in cases.items():
            with self.subTest(case=name):
                info.write_text(valid)
                (self.src / "base" / "BUILD.gn").write_text("patched\n")
                self.sandbox.commit_all("main")
                self.malformed(mutate)
                result, document = self.prepare_build()
                self.assertEqual((result.returncode, document["error"]["code"]), (4, "PREPARATION_CONFLICT"), name)
                self.assertEqual(self.node_calls(), [])
                self.assertEqual((self.src / "base" / "BUILD.gn").read_text(), "my local experiment\n")
                self.assertFalse(self.document("drift")[1]["data"]["metadata_complete"], name)

    def test_patches_in_subdirectories_without_a_repository_list_are_incomplete_evidence(self):
        self.add_repository("v8")
        self.add_nested_patch("v8", "BUILD.gn")
        self.sandbox.commit_all("main")
        self.cfg.unlink()
        result, document = self.document("drift")
        self.assertFalse(document["data"]["metadata_complete"])
        self.assertIn(".repositories.cfg", " ".join(document["data"]["incomplete_reasons"]))
        result, document = self.prepare_build()
        self.assertEqual(document["error"]["code"], "PREPARATION_CONFLICT")
        self.assertEqual(self.node_calls(), [])


if __name__ == "__main__":
    unittest.main()
