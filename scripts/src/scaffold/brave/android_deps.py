# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Android-on-Mac support repository: per-checkout working copies, compatibility, and preparation.

Each checkout owns a working copy of the support repository beside its source
workspace, so two checkouts can use different revisions without switching one
shared tree. Only the Git object cache is shared. Existing working copies are
never reset, switched, or cleaned by the scaffold.
"""

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
from . import freshness, gitstate, support_scripts, sync_scope
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


def cache_path(state_root):
    return store_root(state_root) / "cache" / "android-support.git"


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


def setup_working_copy(identity, state_root, source, ref, log=None):
    """Create or update the shared object cache and this checkout's working copy.

    This is the only step that uses the network (when `source` is remote). An
    existing working copy is only switched to an explicitly requested ref, and
    only when it has no local changes and no unpushed commits.
    """
    source = source or metadata()["url"]
    cache = cache_path(state_root)
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        current = _git(cache, ["remote", "get-url", "origin"], log).stdout.strip()
        if current and current != source:
            raise ScaffoldError("PREPARATION_CONFLICT",
                                "The shared object cache uses %s, not %s." % (current, source),
                                details={"cache": str(cache)})
        fetched = _git(cache, ["fetch", "--prune", "origin", "+refs/*:refs/*"], log, timeout=1800)
        action = "fetched"
    else:
        fetched = run_capture(["git", "clone", "--mirror", source, str(cache)], str(cache.parent), None, log,
                              timeout=3600)
        action = "cloned"
    if fetched.returncode != 0:
        raise ScaffoldError("CHILD_FAILED", "Updating the shared object cache failed: %s" % fetched.stderr.strip()[-500:],
                            details={"cache": str(cache)}, child_exit_code=fetched.returncode)
    wc = working_copy(identity)
    if (wc / ".git").exists() and lfs_pointers(wc, log):
        materialize_lfs(wc, source, cache.parent / "android-support-lfs", log)
    facts = inspect_working_copy(wc, log)
    if wc.exists() and facts is None:
        raise ScaffoldError("OWNERSHIP_CONFLICT",
                            "%s exists but is not a Git working copy; move it aside yourself and repeat." % wc,
                            details={"path": str(wc)})
    if facts is None:
        # A local clone hardlinks the cache's objects; unlike an alternates reference it also works when
        # the cache is shallow and does not break if the cache is later removed.
        cloned = run_capture(["git", "clone", str(cache), str(wc)], str(wc.parent), _no_smudge_env(), log,
                             timeout=3600)
        if cloned.returncode == 0:
            cloned = run_capture(["git", "-C", str(wc), "remote", "set-url", "origin", source], str(wc), None, log,
                                 timeout=60)
        if cloned.returncode != 0:
            raise ScaffoldError("CHILD_FAILED", "Creating the working copy failed: %s" % cloned.stderr.strip()[-500:],
                                details={"path": str(wc)}, child_exit_code=cloned.returncode)
        target = ref or metadata()["default_ref"]
        if ref or target != (inspect_working_copy(wc, log) or {}).get("branch"):
            _checkout(wc, target, log)
        materialize_lfs(wc, source, cache.parent / "android-support-lfs", log)
        created = True
    else:
        created = False
        if ref and ref not in (facts["branch"], facts["head"]):
            if facts["dirty"] or facts["unpushed_commits"]:
                raise ScaffoldError(
                    "PREPARATION_CONFLICT",
                    "The working copy has local changes or unpushed commits, so it was not switched to %s." % ref,
                    details={"path": str(wc), "dirty_files": facts["dirty"][:20],
                             "unpushed_commits": facts["unpushed_commits"]})
            fetch = _git(wc, ["fetch", "origin"], log, timeout=1800)
            if fetch.returncode != 0:
                raise ScaffoldError("CHILD_FAILED", "Fetching into the working copy failed.",
                                    child_exit_code=fetch.returncode)
            _checkout(wc, ref, log)
            materialize_lfs(wc, source, cache.parent / "android-support-lfs", log)
    return {"cache": str(cache), "cache_action": action, "working_copy": inspect_working_copy(wc, log),
            "created": created}


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


def _checkout(wc, ref, log):
    result = run_capture(["git", "-C", str(wc), "checkout", "-q", ref], str(wc), _no_smudge_env(), log, timeout=300)
    if result.returncode != 0:
        raise ScaffoldError("DEPENDENCY_INCOMPATIBLE", "The ref %r does not exist in the support repository." % ref,
                            details={"stderr": result.stderr.strip()[-300:]})


# --- compatibility ----------------------------------------------------------------------------


def run_gate(wc, script, environ, log=None):
    """Run a support script in verify mode. Returns (ok, first error line)."""
    support_scripts.require_contracts(wc)
    result = run_capture(["bash", "./" + script, "-v"], str(wc), environ, log, timeout=300)
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
                        note="Choose a newer or older support revision for this checkout only, then run "
                             "'bdev android setup --ref <ref>' or switch the working copy yourself."),
                 repair(["bdev", "android", "setup", "--checkout", str(identity.core), "--ref", "<ref>"],
                        note="Placeholder ref; switches only a clean working copy.")])


def missing_working_copy(identity, wc):
    return ScaffoldError(
        "DEPENDENCY_INCOMPATIBLE", "The Android-on-Mac support working copy is missing: %s" % wc,
        details={"working_copy": str(wc), "checkout": str(identity.core)},
        repairs=[repair(["bdev", "android", "setup", "--checkout", str(identity.core)],
                        note="Explicit preparation: clones the support repository (network) into this checkout's "
                             "own working copy.")])


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
    if _is_macho(source):
        return _is_macho(copied)
    if source.stat().st_size > MAX_COMPARED_BYTES:
        return False
    return source.read_bytes() == copied.read_bytes()


def resources_current(identity, wc):
    """Whether copied resources match their sources (directories by a small sentinel file)."""
    manifest = resource_manifest(wc)
    if manifest is None:
        return False, "copyMacRes.sh has no recognizable resource declarations"
    for destination, source in manifest:
        origin = Path(wc) / source
        copied = identity.src / destination / origin.name
        if not copied.exists():
            return False, "%s has not been copied" % copied.relative_to(identity.src)
        if origin.is_file() and not _same_small_file(origin, copied):
            return False, "%s differs from its source" % copied.relative_to(identity.src)
        if origin.is_dir():
            sentinel = next((name for name in SENTINELS if (origin / name).is_file()
                             and (origin / name).stat().st_size <= MAX_COMPARED_BYTES
                             and not _is_macho(origin / name)), None)
            if sentinel is None or not _same_small_file(origin / sentinel, copied / sentinel):
                return False, "%s is stale or cannot be compared" % copied.relative_to(identity.src)
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


def recorded_results(identity, state_root):
    """Paths that still hold exactly what the last support preparation wrote there."""
    receipt = _read_state(identity, state_root) or {}
    found = {identity.src / key for key, digest in (receipt.get("targets") or {}).items()
             if digest and _sha(identity.src / key) == digest}
    for key, signatures in (receipt.get("resources") or {}).items():
        destination = identity.src / key
        for relative, signature in signatures.items():
            path = destination / relative
            if os.path.lexists(path) and _stat_signature(path) == signature:
                found.add(path)
    return found


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


def protected_work(identity, wc, receipt, scripts, log=None):
    """Local work that running `scripts` could overwrite, and problems that block deciding."""
    scope = sync_scope.sync_repositories(identity)
    conflicts = [{"path": problem, "reason": "incomplete evidence"} for problem in scope.problems]
    if "applyPatches.sh" in scripts:
        files, problems = write_inventory(identity, wc, log)
        conflicts = problems
        known = (receipt or {}).get("targets", {})
        for repo in {repo for repo, _ in files.values()}:
            paths = {relative: key for key, (owner, relative) in files.items() if owner == repo}
            changes = gitstate.inspect_changes(repo, log, paths)
            for relative in sorted(changes.all & set(paths)):
                key = paths[relative]
                if relative in changes.staged or known.get(key) != _sha(repo / relative):
                    conflicts.append({"path": key, "reason": "has local edits that applying support patches "
                                      "could overwrite"})
    if "copyMacRes.sh" in scripts:
        conflicts += resource_conflicts(identity, wc, (receipt or {}).get("resources") or {}, log)
    return conflicts


def _identical(origin, copied):
    """Whether an existing destination file is a copy of the support file (Mach-O files are re-signed after copying)."""
    if os.path.islink(copied) or not os.path.isfile(copied) or not os.path.isfile(origin):
        return False
    if _is_macho(origin):
        return _is_macho(copied)
    return filecmp.cmp(origin, copied, shallow=False)


def _tracked_and_unmodified(identity, files, log):
    """Which of the given destination files a repository tracks and Git shows unchanged: Chromium's own dependency files."""
    repositories = sync_scope.sync_repositories(identity).repositories
    by_owner = {}
    for path in files:
        owner = owning_repository(repositories, path)
        if owner is not None:
            by_owner.setdefault(owner, []).append(os.path.relpath(path, owner))
    safe = set()
    for owner, relatives in by_owner.items():
        for start in range(0, len(relatives), 200):
            chunk = relatives[start:start + 200]
            tracked = gitstate.tracked_paths(owner, chunk, log)
            safe |= {owner / name for name in tracked - gitstate.changed_paths(owner, tracked, log)}
    return safe


def resource_conflicts(identity, wc, recorded, log=None):
    """Destination files a resource copy would replace that may hold work the scaffold cannot account for.

    A file is the scaffold's to replace when the last refresh left it exactly as recorded, when it is identical to
    the support resource, or when a repository tracks it and Git shows no change (a dependency Chromium supplied).
    A file with a record that no longer matches was changed since. A file with no record, and none of that
    evidence, is reported: an absent record never means an absence of local work.
    """
    conflicts, unknown = [], {}
    for key, origin, copied in resource_destinations(identity, wc):
        if not origin.exists():
            continue
        previous = recorded.get(key) or {}
        for relative, signature in resource_signature(origin, copied).items():
            display = os.path.join(key, relative) if relative else key
            if relative in previous:
                if previous[relative] != signature:
                    conflicts.append({"path": display, "reason": "changed since the last support refresh and would be "
                                      "replaced by the support resource"})
            elif not _identical(origin / relative if relative else origin, copied / relative if relative else copied):
                unknown[copied / relative if relative else copied] = display
    safe = _tracked_and_unmodified(identity, unknown, log) if unknown else set()
    conflicts += [{"path": display, "reason": "exists without a record of where it came from, differs from the support "
                   "resource, and is not an unmodified tracked dependency file; move it aside (sync restores fetched "
                   "dependencies) or restore it, then repeat"}
                  for path, display in sorted(unknown.items(), key=lambda item: item[1]) if path not in safe]
    return conflicts


def plan_preparation(ctx, identity, log=None):
    """Decide which support scripts must run and whether running them could lose local work."""
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
            "%d large file(s) in the support working copy are not materialized (still pointers), so its resources "
            "cannot be copied. Nothing was fetched." % len(pointers),
            details={"working_copy": str(wc), "pointers": pointers[:20]},
            repairs=[repair(["bdev", "android", "setup", "--checkout", str(identity.core)],
                            note="Explicit preparation: fetches the missing large files (network).")])
    evidence = {"working_copy": facts, "version_gate": "passed"}
    receipt = _read_state(identity, ctx.state_root)
    dirty = inputs_dirty(wc, log)
    patched_ok, _ = run_gate(wc, "applyPatches.sh", ctx.environ, log)
    current, why = resources_current(identity, wc)
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
    conflicts = protected_work(identity, wc, receipt, scripts, log)
    declared = [identity.src / key for key in planned_writes(identity, wc, scripts, log)] if not conflicts else []
    return SupportPlan("conflict" if conflicts else "refresh", "; ".join(reasons), evidence, conflicts, scripts,
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
    if "copyMacRes.sh" in plan.scripts:
        resources = {key: resource_signature(origin, copied) for key, origin, copied in
                     resource_destinations(identity, wc)}
    complete = not inputs_dirty(wc, log) and resources_current(identity, wc)[0]
    atomic_write(state_path(identity, state_root), json.dumps({
        "inputs": input_state(wc, log) if complete else None, "targets": targets, "resources": resources},
        indent=1, sort_keys=True))
    return complete


def refresh(ctx, identity, loaded, plan, log=None):
    """Run the support scripts the plan names, then record the result. Mutates the checkout.

    Reviewed identities and preservation checks are revalidated before execution.
    The tracked-change after-check supplies additional evidence of adapter mistakes.
    """
    wc = working_copy(identity)
    support_scripts.require_contracts(wc)
    conflicts = protected_work(identity, wc, _read_state(identity, ctx.state_root), plan.scripts, log)
    declared = tuple(identity.src / key for key in planned_writes(identity, wc, plan.scripts, log))
    if conflicts or declared != plan.declared:
        raise conflict_error(SupportPlan("conflict", "Support inputs changed after planning.", conflicts=conflicts or
                             [{"path": "support manifest", "reason": "write inventory changed after planning"}]), identity)
    scope = sync_scope.sync_repositories(identity)
    before = sync_scope.snapshot(identity, scope, log, include_core=True)
    steps = []
    for script in plan.scripts:
        argv = ["bash", "./" + script]
        code = run_streaming(argv, str(wc), loaded, ctx.log, json_mode=ctx.json_mode)
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
        "Android support preparation could overwrite local work or cannot tell what it writes (%d item(s)); "
        "nothing was changed." % len(plan.conflicts),
        details={"files": plan.conflicts[:50], "total": len(plan.conflicts), "checkout": str(identity.core)},
        repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)],
                        note="Review the listed files. Keep wanted edits elsewhere, then restore the listed "
                             "files (or remove a resource that is only a stale copy) and repeat.")])


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
