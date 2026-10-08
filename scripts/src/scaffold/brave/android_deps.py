# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Optional shared Android-on-Mac support checkout, compatibility, and preparation."""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from ..common.config import atomic_write
from ..common.env import resolves_inside
from ..common.procs import run_capture, run_streaming
from ..common.results import ScaffoldError, repair
from . import freshness, support_scripts, sync_scope
from .patchformat import UnknownPatchFormat, parse_patch_targets
from .patch_inventory import sha256_or_none
from .records import checkout_key, store_root

METADATA = Path(__file__).with_name("android_support.toml")
WORKING_COPY_NAME = "brave-android-mac-support"
SENTINELS = ("release", "cr_build_revision", "source.properties", "sysroot/NOTICE", "NOTICE")
MAX_COMPARED_BYTES = 1 << 20
MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf")


def metadata():
    return tomllib.loads(METADATA.read_text(encoding="utf-8"))["support"]


def working_copy(identity):
    return identity.workspace / WORKING_COPY_NAME


def shared_path(config):
    return config.android_support_path or config.directory / WORKING_COPY_NAME


def require_workspace_link(identity, config):
    wc = working_copy(identity)
    shared = shared_path(config)
    if not wc.is_symlink() or wc.resolve() != shared.resolve():
        raise ScaffoldError(
            "DEPENDENCY_INCOMPATIBLE", "The workspace support path must link to %s; run Android setup." % shared,
            details={"working_copy": str(wc), "shared_checkout": str(shared)},
            repairs=[repair(["bcore", "android", "setup", "--checkout", str(identity.core)])])


def script_environment(wc, environ):
    # Bash needs the workspace link as its logical cwd for the scripts' ../src paths.
    return {**environ, "PWD": str(Path(wc).absolute())}


def _git(path, args, log=None, timeout=300):
    return run_capture(["git", "-C", str(path), *args], str(path), None, log, timeout=timeout)


def inspect_working_copy(path, log=None):
    """Facts about a working copy, or None when the directory is not a Git repository."""
    if not (Path(path) / ".git").exists():
        return None
    head = _git(path, ["rev-parse", "HEAD"], log)
    branch = _git(path, ["symbolic-ref", "--short", "-q", "HEAD"], log)
    status = _git(path, ["status", "--porcelain", "--untracked-files=all"], log)
    unpushed = _git(path, ["rev-list", "--count", "HEAD", "--not", "--remotes"], log)
    origin = _git(path, ["remote", "get-url", "origin"], log)
    return {"path": str(path), "head": head.stdout.strip() or None, "branch": branch.stdout.strip() or None,
            "dirty": status.stdout.splitlines(), "unpushed_commits": int(unpushed.stdout.strip() or 0)
            if unpushed.returncode == 0 else None, "origin": origin.stdout.strip() or None}


# --- explicit acquisition ---------------------------------------------------------------------


def setup_working_copy(identity, config, source, ref, log=None):
    """Prepare one shared checkout and preserve existing workspace copies before linking."""
    for option, value in (("--source", source), ("--ref", ref)):
        if value and value.startswith("-"):
            raise ScaffoldError("INVALID_INPUT", "%s cannot start with '-': Git would read it as an option." % option,
                                details={"option": option})
    shared = shared_path(config).absolute()
    wc = working_copy(identity)
    if shared == wc.absolute() or shared.is_relative_to(wc.absolute()):
        raise ScaffoldError("CONFIG_INVALID", "The shared support path must be outside the workspace support path.")
    if wc.is_symlink() and wc.resolve() != shared.resolve():
        raise ScaffoldError("OWNERSHIP_CONFLICT", "%s links to another location; review it before setup." % wc)
    if not shared.exists():
        shared.parent.mkdir(parents=True, exist_ok=True)
    legacy_links = (wc.is_dir() and not wc.is_symlink()
                    and (wc / "copyMacRes.sh").is_symlink()
                    and (wc / "applyPatches.sh").is_symlink()
                    and all(entry.is_symlink() for entry in wc.iterdir()))
    adopt = (not shared.exists() and wc.exists() and not wc.is_symlink() and not legacy_links
             and wc.stat().st_dev == shared.parent.stat().st_dev)
    backup = None
    if wc.exists() and not wc.is_symlink() and not adopt:
        backup = wc.with_name(wc.name + ".previous")
        if backup.exists() or backup.is_symlink():
            raise ScaffoldError("OWNERSHIP_CONFLICT", "%s already exists; review it before replacing the workspace copy." % backup)
    for path in (shared, wc):
        if path == wc and legacy_links:
            continue
        if path.exists() and inspect_working_copy(path, log) is None:
            raise ScaffoldError("OWNERSHIP_CONFLICT", "%s exists but is not a support Git checkout." % path)
    if adopt:
        shared.parent.mkdir(parents=True, exist_ok=True)
        wc.rename(shared)
        try:
            wc.symlink_to(os.path.relpath(shared, wc.parent), target_is_directory=True)
        except OSError:
            shared.rename(wc)
            raise
    created = not shared.exists()
    source = source or metadata()["url"]
    if created:
        shared.parent.mkdir(parents=True, exist_ok=True)
        result = run_capture(_clone_argv(source, shared), str(shared.parent), _no_smudge_env(), log, timeout=3600)
        if result.returncode:
            raise ScaffoldError("CHILD_FAILED", "Cloning the shared support checkout failed.",
                                child_exit_code=result.returncode)
        _checkout(shared, ref or metadata()["default_ref"], log)
    facts = inspect_working_copy(shared, log)
    resolved_ref = _git(shared, ["rev-parse", "--verify", ref + "^{commit}"], log).stdout.strip() if ref else None
    if ref and ref != facts["branch"] and resolved_ref != facts["head"]:
        if facts["dirty"] or facts["unpushed_commits"] != 0:
            raise ScaffoldError("PREPARATION_CONFLICT", "The shared support checkout has local work; its revision was not changed.",
                                details={"path": str(shared), "dirty_files": facts["dirty"][:20],
                                         "unpushed_commits": facts["unpushed_commits"]})
        result = _git(shared, ["fetch", "origin"], log, timeout=1800)
        if result.returncode:
            raise ScaffoldError("CHILD_FAILED", "Fetching the shared support checkout failed.", child_exit_code=result.returncode)
        _checkout(shared, ref, log)
    materialize_lfs(shared, source, shared / ".git" / "lfs", log)
    if backup:
        wc.rename(backup)
    if not wc.is_symlink():
        try:
            wc.symlink_to(os.path.relpath(shared, wc.parent), target_is_directory=True)
        except OSError:
            if backup: backup.rename(wc)
            raise
    return {"shared_checkout": str(shared), "working_copy": inspect_working_copy(shared, log),
            "workspace_link": str(wc), "preserved_copy": str(backup) if backup else None, "created": created}


def _no_smudge_env():
    return {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}


def _seed_lfs_store(source, store):
    """Hardlink (or copy) large-file objects of a local source repository into the shared store.

    Large-file objects are named by their content hash, so one store can safely serve every working copy.
    """
    objects = Path(str(source)) / ".git" / "lfs" / "objects"
    if not objects.is_dir():
        return
    for path in objects.rglob("*"):
        if not path.is_file():
            continue
        target = store / "objects" / path.relative_to(objects)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)


def uses_lfs(wc):
    try:
        return "filter=lfs" in (Path(wc) / ".gitattributes").read_text(encoding="utf-8")
    except OSError:
        return False


def lfs_pointers(wc, log=None):
    """Large files that are still pointers in the working copy. Read-only; never fetches.

    Raises when git-lfs cannot list them: an unknown state is not treated as materialized.
    """
    if not uses_lfs(wc):
        return []
    listing = _git(wc, ["lfs", "ls-files"], log)
    if listing.returncode != 0 or listing.truncated:
        raise ScaffoldError("LOCAL_TOOL_MISSING", "git-lfs could not list the support repository's large files: %s" %
                            (listing.stderr.strip()[-300:] or "exit %d" % listing.returncode),
                            details={"path": str(wc)}, repairs=[repair(["brew", "install", "git-lfs"],
                                                                       requires_user_action=True)])
    pointers = []
    for line in listing.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == "-":
            pointers.append(parts[2])
    return pointers


def materialize_lfs(wc, source, store, log=None):
    """Make every large file real content: use the shared store, fetch what it lacks, then verify.

    `git lfs checkout` succeeds while leaving a pointer for content that is not local, so success is judged
    by listing the files that are still pointers. Fetching uses the network and is only for explicit setup.
    """
    if not uses_lfs(wc):
        return
    previous = _git(wc, ["config", "--get", "lfs.storage"], log).stdout.strip()
    if previous:
        previous = Path(previous)
        if not previous.is_absolute(): previous = Path(wc) / previous
        if previous.resolve() != store.resolve() and (previous / "objects").is_dir():
            for path in (previous / "objects").rglob("*"):
                if not path.is_file(): continue
                target = store / "objects" / path.relative_to(previous / "objects")
                if target.exists(): continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try: os.link(path, target)
                except OSError: shutil.copy2(path, target)
    store.mkdir(parents=True, exist_ok=True)
    if os.path.isdir(str(source)):
        _seed_lfs_store(source, store)
    _git(wc, ["config", "lfs.storage", str(store)], log)
    _git(wc, ["lfs", "install", "--local"], log)
    _git(wc, ["lfs", "checkout"], log, timeout=1800)
    if not lfs_pointers(wc, log):
        return
    pull = _git(wc, ["lfs", "pull"], log, timeout=3600)
    remaining = lfs_pointers(wc, log)
    if pull.returncode != 0 or remaining:
        raise ScaffoldError(
            "CHILD_FAILED", "%d large file(s) in the support working copy are still pointers after fetching: %s" % (
                len(remaining), (pull.stderr.strip() or "the content is not available from the remote")[-300:]),
            details={"path": str(wc), "pointers": remaining[:20]}, child_exit_code=pull.returncode or 0,
            repairs=[repair(["git", "-C", str(wc), "lfs", "pull"],
                            note="Uses the network and your Git credentials; run it after fixing access.")])


def _clone_argv(source, shared):
    return ["git", "clone", "--", source, str(shared)]


def _checkout(wc, ref, log):
    result = run_capture(["git", "-C", str(wc), "checkout", "-q", ref, "--"], str(wc), _no_smudge_env(), log,
                         timeout=300)
    if result.returncode != 0:
        raise ScaffoldError("DEPENDENCY_INCOMPATIBLE", "The ref %r does not exist in the support repository." % ref,
                            details={"stderr": result.stderr.strip()[-300:]})


# --- compatibility ----------------------------------------------------------------------------


def run_gate(wc, script, environ, log=None):
    """Run a support script in verify mode. Returns (ok, first error line)."""
    support_scripts.require_contracts(wc)
    result = run_capture(["bash", "./" + script, "-v"], str(wc), script_environment(wc, environ), log, timeout=300)
    if result.returncode == 0:
        return True, None
    output = result.stdout + result.stderr
    match = re.search(r"Error: (.*)", output)
    return False, (match.group(1) if match else output.strip()[-300:] or "exit %d" % result.returncode)


def incompatible(identity, wc, detail, facts=None):
    return ScaffoldError(
        "DEPENDENCY_INCOMPATIBLE",
        "The Android-on-Mac support revision in %s does not match this checkout: %s" % (wc, detail),
        details={"working_copy": facts or {"path": str(wc)}, "checkout": str(identity.core), "reason": detail},
        repairs=[repair(["git", "-C", str(wc), "log", "--oneline", "-n", "10"],
                        note="Choose a newer or older support revision for every linked checkout, then run "
                             "'bcore android setup --ref <ref>' or switch the working copy yourself."),
                 repair(["bcore", "android", "setup", "--checkout", str(identity.core), "--ref", "<ref>"],
                        note="Placeholder ref; switches only a clean working copy.")])


def missing_working_copy(identity, wc):
    return ScaffoldError(
        "DEPENDENCY_INCOMPATIBLE", "The Android-on-Mac support working copy is missing: %s" % wc,
        details={"working_copy": str(wc), "checkout": str(identity.core)},
        repairs=[repair(["bcore", "android", "setup", "--checkout", str(identity.core)],
                        note="Explicit preparation: clones the support repository (network) into one shared working copy.")])


# --- resources and currency --------------------------------------------------------------------


def resource_manifest(wc):
    return support_scripts.require_contract(wc, "copyMacRes.sh")["resources"]


def _is_macho(path):
    try:
        with open(path, "rb") as stream:
            return stream.read(4) in MACHO_MAGICS
    except OSError:
        return False


def _same_small_file(source, copied):
    if not (source.is_file() and copied.is_file()):
        return False
    if source.stat().st_size > MAX_COMPARED_BYTES:
        return False
    return source.read_bytes() == copied.read_bytes()


def resources_current(identity, wc, receipt=None):
    """Whether copied resources match their sources (directories by a small sentinel file)."""
    manifest = resource_manifest(wc)
    if manifest is None:
        return False, "copyMacRes.sh has no recognizable resource declarations"
    for destination, source in manifest:
        origin = Path(wc) / source
        copied = identity.src / destination / origin.name
        if not copied.exists():
            return False, "%s has not been copied" % copied.relative_to(identity.src)
        if origin.is_file() and not _identical(origin, copied):
            key = os.path.relpath(copied, identity.src)
            previous = (receipt or {}).get("resources", {}).get(key, {})
            source_digest = (receipt or {}).get("resource_sources", {}).get(key)
            if not source_digest or source_digest != _sha(origin) or previous != resource_signature(origin, copied):
                if previous and previous != resource_signature(origin, copied):
                    return False, "%s differs from its support source and changed since the last recorded copy" % key
                if previous and source_digest and source_digest != _sha(origin):
                    return False, "%s has a support source that changed since the last recorded copy" % key
                return False, "%s differs from its support source; no saved copy record explains the difference (origin unknown)" % key
        if origin.is_dir():
            sentinel = next((name for name in SENTINELS if (origin / name).is_file()
                             and (origin / name).stat().st_size <= MAX_COMPARED_BYTES
                             and not _is_macho(origin / name)), None)
            if sentinel is None or not _same_small_file(origin / sentinel, copied / sentinel):
                return False, "%s is stale or cannot be compared" % copied.relative_to(identity.src)
    contract = support_scripts.require_contract(wc, "copyMacRes.sh")
    for source, destination in contract.get("checkout_resources", []):
        origin, copied = identity.src / source, identity.src / destination
        if not resolves_inside(origin, identity.src) or not resolves_inside(copied, identity.src):
            raise ScaffoldError("PREPARATION_CONFLICT", "The checkout resource leaves its declared location: " + destination)
        if not origin.exists() or not copied.exists() or copied.is_symlink():
            return False, destination + " has not been copied from the synced checkout"
        if origin.is_dir():
            for path in origin.rglob("*"):
                if path.is_file():
                    target = copied / path.relative_to(origin)
                    # macOS rsync preserves whole seconds, but drops nanoseconds.
                    if not target.is_file() or (path.stat().st_size, int(path.stat().st_mtime)) != (
                            target.stat().st_size, int(target.stat().st_mtime)):
                        return False, destination + " differs from the synced checkout"
        elif not _identical(origin, copied):
            return False, destination + " differs from the synced checkout"
    for relative in contract.get("required_resources", []):
        path = identity.src / relative
        if not resolves_inside(path, identity.src):
            raise ScaffoldError("PREPARATION_CONFLICT", "The resource leaves its declared location: " + relative)
        if not path.is_file():
            return False, relative + " is missing"
    return True, None


def state_path(identity, state_root):
    return store_root(state_root) / "state" / checkout_key(identity.core) / "android-support.json"


def input_state(wc, log=None):
    """Git object ids of the inputs the support scripts read, from the working copy's HEAD."""
    manifest = resource_manifest(wc) or []
    paths = ["copyMacRes.sh", "applyPatches.sh", "patches", *sorted({source for _, source in manifest})]
    objects = {}
    for path in paths:
        result = _git(wc, ["rev-parse", "--verify", "HEAD:" + path], log)
        objects[path] = result.stdout.strip() if result.returncode == 0 else None
    return {"head": _git(wc, ["rev-parse", "HEAD"], log).stdout.strip(), "objects": objects}


def inputs_dirty(wc, log=None):
    manifest = resource_manifest(wc) or []
    paths = ["copyMacRes.sh", "applyPatches.sh", "patches", ".gitattributes", *sorted({s for _, s in manifest})]
    status = _git(wc, ["status", "--porcelain", "--untracked-files=all", "--", *paths], log)
    return status.stdout.splitlines() if status.returncode == 0 else ["status unavailable"]


def owning_repository(repositories, path):
    """The most deeply nested repository that contains `path`."""
    return max((repo for repo in repositories if Path(path).is_relative_to(repo)), key=lambda repo: len(repo.parts),
               default=None)


def _patch_targets(wc, name, problems):
    try:
        return parse_patch_targets((Path(wc) / "patches" / name).read_text(encoding="utf-8"))
    except (UnknownPatchFormat, OSError, UnicodeDecodeError) as error:
        problems.append({"path": "patches/" + name, "reason": "patch format not recognised (%s)" % error})
        return set()


def write_inventory(identity, wc, log=None):
    """Source writes from the reviewed script contract and its current patch files.

    Shell code is identified before execution; patch data supplies only paths
    within the repositories the manifest names. Incomplete repository discovery
    blocks even when a patch names an existing repository.
    """
    contract = support_scripts.require_contract(wc, "applyPatches.sh")
    scope = sync_scope.sync_repositories(identity)
    files = {}
    problems = [{"path": problem, "reason": "incomplete evidence"} for problem in scope.problems]
    for relative_repository, name in contract["patches"]:
        repo = identity.src / relative_repository
        if repo not in scope.repositories:
            problems.append({"path": relative_repository or ".", "reason": "patch repository is not in the complete sync inventory"})
            continue
        for target in _patch_targets(wc, name, problems):
            files[os.path.relpath(repo / target, identity.src)] = (repo, target)
    for relative in contract["direct"]:
        owner = owning_repository(scope.repositories, identity.src / relative)
        if owner is None:
            problems.append({"path": relative, "reason": "owning repository is unknown"})
        else:
            files[relative] = (owner, os.path.relpath(identity.src / relative, owner))
    return files, problems


def _sha(path):
    return sha256_or_none(path)


def _stat_signature(path):
    status = os.lstat(path)
    return [status.st_size, status.st_mtime_ns]


def resource_signature(origin, copied):
    """Size and modification time of destination files copying or signing could change."""
    origin, copied = Path(origin), Path(copied)
    if origin.is_dir() and not origin.is_symlink():
        relatives = set()
        for base in (origin, copied):
            for current, directories, files in os.walk(base):
                names = [*files, *(name for name in directories if Path(current, name).is_symlink())]
                relatives.update(os.path.relpath(Path(current, name), base) for name in names)
        relatives = sorted(relatives)
    else:
        relatives = [""]
    return {relative: _stat_signature(copied / relative) for relative in relatives
            if os.path.lexists(copied / relative)}


def resource_destinations(identity, wc):
    """(destination path relative to the source root, source path, destination path) for each resource."""
    found = []
    for destination, source in resource_manifest(wc) or []:
        origin = Path(wc) / source
        copied = identity.src / destination / origin.name
        if not resolves_inside(copied, identity.src) or (origin.is_dir() and copied.is_symlink()):
            raise ScaffoldError("PREPARATION_CONFLICT", "The resource destination %s leaves its declared "
                                "checkout location; nothing was copied." % copied,
                                details={"files": [{"path": str(copied), "reason": "linked resource destination"}]})
        found.append((os.path.relpath(copied, identity.src), origin, copied))
    contract = support_scripts.require_contract(wc, "copyMacRes.sh")
    for source, destination in contract.get("checkout_resources", []):
        origin, copied = identity.src / source, identity.src / destination
        if not resolves_inside(origin, identity.src) or not resolves_inside(copied, identity.src) or copied.is_symlink():
            raise ScaffoldError("PREPARATION_CONFLICT", "The checkout resource leaves its declared location: " + destination)
        found.append((destination, origin, copied))
    return found


def freshness_inputs(identity, log=None):
    """Support inputs an APK depends on: the working copy's revision and edits, and the copied resources."""
    wc = working_copy(identity)
    if not (wc / ".git").exists():
        return {"support_head": None, "support_worktree": None, "support_resources": None}
    digest = hashlib.sha256()
    for key, origin, copied in resource_destinations(identity, wc):
        digest.update(("%s=%s\n" % (key, json.dumps(resource_signature(origin, copied), sort_keys=True))).encode())
    return {"support_head": freshness.resolve_head(wc, log), "support_worktree": freshness.worktree_state(wc, log),
            "support_resources": digest.hexdigest()}


def planned_writes(identity, wc, scripts, log=None):
    """Source-relative paths the given support scripts would write (read-only)."""
    writes = []
    if "applyPatches.sh" in scripts:
        writes += sorted(write_inventory(identity, wc, log)[0])
    if "copyMacRes.sh" in scripts:
        writes += [key for key, _, _ in resource_destinations(identity, wc)]
    return writes


class SupportPlan:
    """What preparation must do. `scripts` are the support scripts to run, in order."""

    def __init__(self, action, reason, evidence=None, conflicts=None, scripts=(), declared=()):
        self.action, self.reason, self.evidence = action, reason, evidence or {}
        self.conflicts, self.scripts = conflicts or [], tuple(scripts)
        self.declared = tuple(declared)  # absolute paths (files or directories) the scripts are declared to write


def scope_conflicts(identity, wc, scripts, log=None):
    """Reject incomplete write inventories, without judging destination file origin."""
    scope = sync_scope.sync_repositories(identity)
    conflicts = [{"path": problem, "reason": "incomplete evidence"} for problem in scope.problems]
    if "applyPatches.sh" in scripts:
        _, problems = write_inventory(identity, wc, log)
        conflicts.extend(problems)
    return conflicts


def _identical(origin, copied):
    """Whether an existing destination has exactly the support file bytes."""
    if os.path.islink(copied) or not os.path.isfile(copied) or not os.path.isfile(origin):
        return False
    return filecmp.cmp(origin, copied, shallow=False)


def plan_preparation(ctx, identity, log=None):
    """Decide which support scripts must run within their reviewed write scope."""
    require_workspace_link(identity, ctx.config)
    wc = working_copy(identity)
    facts = inspect_working_copy(wc, log)
    if facts is None:
        raise missing_working_copy(identity, wc)
    support_scripts.require_contracts(wc)
    ok, detail = run_gate(wc, "copyMacRes.sh", ctx.environ, log)
    if not ok:
        raise incompatible(identity, wc, detail, facts)
    pointers = lfs_pointers(wc, log)
    if pointers:
        raise ScaffoldError(
            "DEPENDENCY_INCOMPATIBLE",
            "%d large file(s) in the support working copy are still Git LFS pointers rather than downloaded files, so its resources "
            "cannot be copied. Nothing was fetched." % len(pointers),
            details={"working_copy": str(wc), "pointers": pointers[:20]},
            repairs=[repair(["bcore", "android", "setup", "--checkout", str(identity.core)],
                            note="Explicit preparation: fetches the missing large files (network).")])
    evidence = {"working_copy": facts, "version_gate": "passed"}
    receipt = _read_state(identity, ctx.state_root)
    dirty = inputs_dirty(wc, log)
    patched_ok, _ = run_gate(wc, "applyPatches.sh", ctx.environ, log)
    current, why = resources_current(identity, wc, receipt)
    recorded = bool(receipt) and receipt.get("inputs") == input_state(wc, log)
    if patched_ok and current and recorded and not dirty:
        return SupportPlan("current", "Support patches and resources are current.", evidence)
    reasons = []
    if dirty:
        reasons.append("support inputs in the working copy have local changes")
    if not patched_ok:
        reasons.append("support patches are not applied")
    if not current:
        reasons.append(why)
    if not recorded:
        reasons.append("no matching record of a refresh for this checkout")
    settled = recorded and not dirty
    scripts = [name for name, done in (("applyPatches.sh", patched_ok), ("copyMacRes.sh", current))
               if not (done and settled)]
    conflicts = scope_conflicts(identity, wc, scripts, log)
    if ctx.parsed.get("skip_support_refresh"):
        conflicts.append({"path": str(wc), "reason": "refresh is needed but --skip-support-refresh was set"})
    declared = [identity.src / key for key in planned_writes(identity, wc, scripts, log)] if not conflicts else []
    return SupportPlan("conflict" if conflicts else "refresh",
                       "; ".join(reasons + [item["reason"] for item in conflicts]), evidence, conflicts, scripts,
                       declared)


def _read_state(identity, state_root):
    try:
        return json.loads(state_path(identity, state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record_state(identity, state_root, plan, log=None):
    """Remember what the scripts just run wrote, so later edits can be told apart from support's own changes."""
    wc = working_copy(identity)
    previous = _read_state(identity, state_root) or {}
    targets, resources = dict(previous.get("targets", {})), dict(previous.get("resources", {}))
    if "applyPatches.sh" in plan.scripts:
        files, _ = write_inventory(identity, wc, log)
        targets = {key: _sha(repo / relative) for key, (repo, relative) in files.items()}
    resource_sources = dict(previous.get("resource_sources", {}))
    if "copyMacRes.sh" in plan.scripts:
        resources, resource_sources = {}, {}
        for key, origin, copied in resource_destinations(identity, wc):
            resources[key] = resource_signature(origin, copied)
            if origin.is_file():
                resource_sources[key] = _sha(origin)
    receipt = {"resources": resources, "resource_sources": resource_sources}
    complete = not inputs_dirty(wc, log) and resources_current(identity, wc, receipt)[0]
    atomic_write(state_path(identity, state_root), json.dumps({
        "inputs": input_state(wc, log) if complete else None, "targets": targets, **receipt},
        indent=1, sort_keys=True))
    return complete


def refresh(ctx, identity, loaded, plan, log=None):
    """Run the support scripts the plan names, then record the result. Mutates the checkout.

    Reviewed identities and write inventories are revalidated before execution.
    The tracked-change after-check supplies additional evidence of adapter mistakes.
    """
    wc = working_copy(identity)
    support_scripts.require_contracts(wc)
    conflicts = scope_conflicts(identity, wc, plan.scripts, log)
    declared = tuple(identity.src / key for key in planned_writes(identity, wc, plan.scripts, log))
    if conflicts or declared != plan.declared:
        raise conflict_error(SupportPlan("conflict", "Support inputs changed after planning.", conflicts=conflicts or
                             [{"path": "support manifest", "reason": "write inventory changed after planning"}]), identity)
    scope = sync_scope.sync_repositories(identity)
    before = sync_scope.snapshot(identity, scope, log, include_core=True)
    steps = []
    for script in plan.scripts:
        argv = ["bash", "./" + script]
        code = run_streaming(argv, str(wc), script_environment(wc, loaded), ctx.log, json_mode=ctx.json_mode)
        steps.append({"argv": argv, "cwd": str(wc), "exit": code})
        if code != 0:
            raise ScaffoldError("CHILD_FAILED", "%s failed (exit %d)." % (script, code),
                                details={"argv": argv, "cwd": str(wc)}, child_exit_code=code)
    changed = sync_scope.changed_between(identity, before, sync_scope.snapshot(identity, scope, log, include_core=True))
    strays = sorted(str(path.relative_to(identity.src)) for path in changed
                    if not any(path == declared or path.is_relative_to(declared) for declared in plan.declared))
    if strays:
        raise ScaffoldError(
            "PREPARATION_CONFLICT",
            "Support preparation changed %d tracked file(s) outside what it declares, so its effects are not "
            "fully understood and nothing was recorded as prepared." % len(strays),
            details={"files": [{"path": path, "reason": "changed by a support script but not declared by it"}
                               for path in strays[:50]], "total": len(strays), "checkout": str(identity.core)},
            repairs=[repair(["git", "-C", str(identity.src), "status", "--short"],
                            note="Review what the script changed; nothing was reverted.")])
    recorded = record_state(identity, ctx.state_root, plan, log)
    return {"steps": steps, "recorded": recorded}


def conflict_error(plan, identity):
    return ScaffoldError(
        "PREPARATION_CONFLICT",
        "Android support preparation is blocked. Review the paths below. Nothing was changed by the support scripts.",
        details={"files": plan.conflicts[:50], "total": len(plan.conflicts), "checkout": str(identity.core)},
        repairs=([repair(["bcore", "build", "android", "--checkout", str(identity.core)],
                         note="Omit --skip-support-refresh only if you want the listed support files replaced.")]
                 if plan.conflicts and all("--skip-support-refresh" in item["reason"] for item in plan.conflicts)
                 else [repair(["bcore", "doctor", "android", "--checkout", str(identity.core)],
                              note="Inspect support readiness. Missing repository data or unknown scripts must be "
                                   "resolved before refresh can run; retrying a build alone will not fix them.")]))


# --- GN overrides ----------------------------------------------------------------------------

BEGIN = "# BEGIN scaffold Android-on-Mac overrides"
END = "# END scaffold Android-on-Mac overrides"
OVERRIDES = ("is_component_build=false", "enable_android_secondary_abi=false", "use_remoteexec=true",
             "use_mold=false", 'android_static_analysis="off"')


def ensure_args_gn(identity, output_dir, chosen=()):
    """Keep a marked block of GN overrides in the output's args.gn; other content is untouched.

    The block follows Core's generated arguments, so it would win over them; settings the caller chose
    for this build (forwarded `--gn` values or `--use_remoteexec`) are left out of it.
    """
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ScaffoldError("OWNERSHIP_CONFLICT", "%s is a symlink; args.gn was not changed." % output_dir)
    args = output_dir / "args.gn"
    text = args.read_text(encoding="utf-8") if args.exists() else ""
    lines, skipping = [], False
    for line in text.splitlines():
        if line == BEGIN:
            skipping = True
        elif line == END:
            skipping = False
        elif not skipping:
            lines.append(line)
    relative = os.path.relpath(output_dir, identity.src)
    header = 'import("//%s/args_generated.gni")' % relative
    if header not in lines:
        lines = [header, ""] + lines
    while lines and not lines[-1]:
        lines.pop()
    overrides = [item for item in OVERRIDES if item.partition("=")[0] not in chosen]
    updated = "\n".join([*lines, "", BEGIN, *overrides, END]) + "\n"
    if updated != text:
        atomic_write(args, updated)
        return True
    return False
