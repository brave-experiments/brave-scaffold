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

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path

from ..common.config import atomic_write
from ..common.procs import run_capture, run_streaming
from ..common.results import ScaffoldError, repair
from .records import checkout_key, store_root

METADATA = Path(__file__).with_name("android_support.toml")
WORKING_COPY_NAME = "brave-android-mac-support"
SENTINELS = ("release", "cr_build_revision", "source.properties", "sysroot/NOTICE", "NOTICE")
MAX_COMPARED_BYTES = 1 << 20
MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf")
PATCH_TARGET = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)
RESOURCE_LINE = re.compile(r'^\s*patch_dependency\s+"[^"]*"\s+"([^"]+)"\s+"[^"]*"\s+"(res/[^"]+)"', re.MULTILINE)


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
    facts = inspect_working_copy(wc, log)
    if wc.exists() and facts is None:
        raise ScaffoldError("OWNERSHIP_CONFLICT",
                            "%s exists but is not a Git working copy; move it aside yourself and repeat." % wc,
                            details={"path": str(wc)})
    if facts is None:
        cloned = run_capture(["git", "clone", "--reference", str(cache), source, str(wc)], str(wc.parent), None, log,
                             timeout=3600)
        if cloned.returncode != 0:
            raise ScaffoldError("CHILD_FAILED", "Creating the working copy failed: %s" % cloned.stderr.strip()[-500:],
                                details={"path": str(wc)}, child_exit_code=cloned.returncode)
        target = ref or metadata()["default_ref"]
        if ref or target != (inspect_working_copy(wc, log) or {}).get("branch"):
            _checkout(wc, target, log)
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
    return {"cache": str(cache), "cache_action": action, "working_copy": inspect_working_copy(wc, log),
            "created": created}


def _checkout(wc, ref, log):
    result = _git(wc, ["checkout", "-q", ref], log)
    if result.returncode != 0:
        raise ScaffoldError("DEPENDENCY_INCOMPATIBLE", "The ref %r does not exist in the support repository." % ref,
                            details={"stderr": result.stderr.strip()[-300:]})


# --- compatibility ----------------------------------------------------------------------------


def run_gate(wc, script, environ, log=None):
    """Run a support script in verify mode. Returns (ok, first error line)."""
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
    try:
        text = (Path(wc) / "copyMacRes.sh").read_text(encoding="utf-8")
    except OSError:
        return None
    entries = RESOURCE_LINE.findall(text)
    return entries or None


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


def patch_targets(wc):
    targets = set()
    for patch in sorted((Path(wc) / "patches").glob("*.patch")):
        targets.update(PATCH_TARGET.findall(patch.read_text(encoding="utf-8", errors="replace")))
    return sorted(targets)


def _sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class SupportPlan:
    def __init__(self, action, reason, evidence=None, conflicts=None):
        self.action, self.reason, self.evidence, self.conflicts = action, reason, evidence or {}, conflicts or []


def plan_preparation(ctx, identity, log=None):
    """Decide whether support patches and resources need refreshing and whether that is safe."""
    wc = working_copy(identity)
    facts = inspect_working_copy(wc, log)
    if facts is None:
        raise missing_working_copy(identity, wc)
    ok, detail = run_gate(wc, "copyMacRes.sh", ctx.environ, log)
    if not ok:
        raise incompatible(identity, wc, detail, facts)
    evidence = {"working_copy": facts, "version_gate": "passed"}
    receipt = _read_state(identity, ctx.state_root)
    dirty = inputs_dirty(wc, log)
    patched_ok, _ = run_gate(wc, "applyPatches.sh", ctx.environ, log)
    current, why = resources_current(identity, wc)
    recorded = receipt and receipt.get("inputs") == input_state(wc, log)
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
    conflicts = []
    if not patched_ok:
        known = (receipt or {}).get("targets", {})
        for target in patch_targets(wc):
            status = _git(identity.src, ["status", "--porcelain", "--", target], log)
            if status.stdout.strip() and known.get(target) != _sha(identity.src / target):
                conflicts.append({"path": target, "reason": "has local edits that applying support patches could "
                                  "overwrite"})
    if conflicts:
        return SupportPlan("conflict", "; ".join(reasons), evidence, conflicts)
    return SupportPlan("refresh", "; ".join(reasons), evidence)


def _read_state(identity, state_root):
    try:
        return json.loads(state_path(identity, state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record_state(identity, state_root, log=None):
    wc = working_copy(identity)
    if inputs_dirty(wc, log):
        return False
    current, _ = resources_current(identity, wc)
    if not current:
        return False
    atomic_write(state_path(identity, state_root), json.dumps({
        "inputs": input_state(wc, log),
        "targets": {target: _sha(identity.src / target) for target in patch_targets(wc)}}, indent=1, sort_keys=True))
    return True


def refresh(ctx, identity, loaded, log=None):
    """Apply support patches and copy resources, then record the result. Mutates the checkout."""
    wc = working_copy(identity)
    steps = []
    for script in ("applyPatches.sh", "copyMacRes.sh"):
        argv = ["bash", "./" + script]
        code = run_streaming(argv, str(wc), loaded, ctx.log, json_mode=ctx.json_mode)
        steps.append({"argv": argv, "cwd": str(wc), "exit": code})
        if code != 0:
            raise ScaffoldError("CHILD_FAILED", "%s failed (exit %d)." % (script, code),
                                details={"argv": argv, "cwd": str(wc)}, child_exit_code=code)
    recorded = record_state(identity, ctx.state_root, log)
    return {"steps": steps, "recorded": recorded}


def conflict_error(plan, identity):
    return ScaffoldError(
        "PREPARATION_CONFLICT",
        "Applying Android support patches could overwrite local edits in %d file(s); nothing was changed."
        % len(plan.conflicts), details={"files": plan.conflicts[:50], "checkout": str(identity.core)},
        repairs=[repair(["bdev", "drift", "--diff", "--checkout", str(identity.core)])])


# --- GN overrides ----------------------------------------------------------------------------

BEGIN = "# BEGIN scaffold Android-on-Mac overrides"
END = "# END scaffold Android-on-Mac overrides"
OVERRIDES = ("is_component_build=false", "enable_android_secondary_abi=false", "use_remoteexec=true",
             "use_mold=false", 'android_static_analysis="off"')


def ensure_args_gn(identity, output_dir):
    """Keep a marked block of GN overrides in the output's args.gn; other content is untouched."""
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
    updated = "\n".join([*lines, "", BEGIN, *OVERRIDES, END]) + "\n"
    if updated != text:
        atomic_write(args, updated)
        return True
    return False
