# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only readiness checks for macOS builds and remote build execution.

RBE checks inspect local configuration only. They never contact a service and
never claim that a VPN or remote endpoint is reachable.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..common.checks import BLOCKER, NOT_CHECKED, PASS, WARNING, make_check
from ..common.procs import run_capture
from ..common.redaction import redact_url_credentials
from ..common.results import ScaffoldError, repair

MIN_FREE_BYTES = 150 * 1024 ** 3
RBE_KEYS = ("rbe_service", "rbe_tls_client_auth_cert", "rbe_tls_client_auth_key", "siso_cache_dir", "use_remoteexec")
METAL_MOUNTS = "/private/var/run/com.apple.security.cryptexd/mnt"


def redact(value):
    """Hide credentials embedded in a URL-like value."""
    return redact_url_credentials(value)


def read_env(path, seen=None):
    """Parse KEY=VALUE lines, expanding `include_env=` files. Raises OSError, ValueError."""
    seen = seen or set()
    real = os.path.realpath(path)
    if real in seen:
        raise ValueError("repeated include")
    seen.add(real)
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "include_env" and separator:
            values.update(read_env(Path(path).parent / value.split("#")[0].strip(), seen))
        elif separator:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def _machine(ctx, scope):
    checks = []
    sdk = run_capture(["xcrun", "--show-sdk-version"], os.getcwd(), ctx.environ, ctx.log, timeout=30)
    version = sdk.stdout.strip()
    checks.append(make_check(
        "macos-sdk", PASS if sdk.returncode == 0 and version else BLOCKER,
        "macOS SDK %s is visible through xcrun." % version if version else "The macOS SDK is not visible through xcrun.",
        scope, affects=("mac build", "mac test"),
        repairs=[] if version else [repair(["xcodebuild", "-runFirstLaunch"], requires_user_action=True,
                                            note="Select full Xcode and accept its license first.")]))
    metal = run_capture(["xcrun", "metal", "--version"], os.getcwd(), ctx.environ, ctx.log, timeout=30)
    mounted = sorted(Path(METAL_MOUNTS).glob("com.apple.MobileAsset.MetalToolchain-*")) \
        if Path(METAL_MOUNTS).is_dir() else []
    if metal.returncode == 0:
        checks.append(make_check("metal-toolchain", PASS, "xcrun metal works.", scope, required=False,
                                 affects=("mac build",), xcrun_works=True))
    elif mounted:
        checks.append(make_check("metal-toolchain", PASS, "A Metal toolchain component is mounted; builds select it.",
                                 scope, required=False, affects=("mac build",)))
    else:
        checks.append(make_check(
            "metal-toolchain", WARNING, "No usable Metal toolchain was found; shader compilation may fail.", scope,
            required=False, affects=("mac build",),
            repairs=[repair(["xcodebuild", "-downloadComponent", "MetalToolchain"], requires_user_action=True)]))
    return checks


def _disk(scope, path):
    free = shutil.disk_usage(path).free
    gigabytes = free / 1000 ** 3
    enough = free >= MIN_FREE_BYTES
    return make_check("disk-space", PASS if enough else WARNING,
                      "%.0f GB free under %s." % (gigabytes, path) if enough else
                      "Only %.0f GB free under %s; a build may need more." % (gigabytes, path),
                      scope, required=False, affects=("mac build",), free_bytes=free)


def _selection(ctx):
    """The selected checkout, or (None, guidance error)."""
    try:
        return ctx.identity(required=True, validate=False), None
    except ScaffoldError as error:
        if error.code in ("CHECKOUT_REQUIRED", "CHECKOUT_AMBIGUOUS"):
            return None, error
        raise


def _unchecked(names, scope, error, required, affects):
    return [make_check(name, NOT_CHECKED, "No checkout is selected: %s" % error.message, scope, required=required,
                       affects=affects, repairs=error.repairs, **error.details) for name in names]


def _services_key(identity, scope):
    env_file = identity.core / ".env"
    problem = None
    try:
        value = read_env(env_file).get("brave_services_key", "")
    except (OSError, ValueError, UnicodeDecodeError):
        value, problem = "", "Cannot read %s or its includes." % env_file
    if value.strip():
        return make_check("services-key", PASS, "brave_services_key is nonempty in %s. Its validity is not verified."
                          % env_file, scope, required=False, affects=("services-backed features",), nonempty=True)
    return make_check("services-key", WARNING, problem or "Set a nonempty brave_services_key in %s." % env_file, scope,
                      required=False, affects=("services-backed features",), nonempty=False)


def _rbe_config(ctx, identity, scope, required):
    """Local RBE/Siso configuration only; no network access."""
    affects = ("rbe build",)
    src, env_file = identity.src, identity.core / ".env"

    def make(name, ok, good, bad, repairs=None, **evidence):
        return make_check(name, PASS if ok else (BLOCKER if required else WARNING), good if ok else bad, scope,
                          required=required, affects=affects, repairs=[] if ok else (repairs or []), **evidence)

    sync = [repair(["bpm", "--checkout", str(identity.core), "run", "sync"],
                   note="Refreshes RBE sync artifacts; needs the internal VPN and changes the checkout.")]
    try:
        env = read_env(env_file)
    except (OSError, ValueError, UnicodeDecodeError):
        return [make("rbe-env", False, "", "Cannot read %s. Use --offline to compile locally." % env_file,
                     file=str(env_file))]
    checks = []
    missing = [key for key in RBE_KEYS if not env.get(key)]
    enabled = env.get("use_remoteexec") == "true" and bool(env.get("rbe_service"))
    checks.append(make("rbe-env", not missing and enabled, "RBE keys are set in %s." % env_file,
                       "Missing or disabled in %s: %s. Use --offline to compile locally." % (
                           env_file, ", ".join(missing) or "use_remoteexec is not true"),
                       missing=missing, rbe_service=redact(env.get("rbe_service", ""))))
    siso_ok = env.get("use_siso", "true") == "true" and env.get("use_reclient", "false") == "false"
    checks.append(make("rbe-siso-mode", siso_ok, "Native Siso mode (use_siso=true, use_reclient=false).",
                       "use_siso/use_reclient select a mode other than native Siso.",
                       use_siso=env.get("use_siso"), use_reclient=env.get("use_reclient")))
    files = {"cert": env.get("rbe_tls_client_auth_cert"), "key": env.get("rbe_tls_client_auth_key")}
    unreadable = [label for label, path in files.items() if not path or not os.access(path, os.R_OK)]
    checks.append(make("rbe-tls-files", not unreadable, "TLS client certificate and key are readable.",
                       "TLS %s not readable. File contents are never read or shown." % " and ".join(unreadable),
                       paths={label: path for label, path in files.items()}))
    cert = files["cert"]
    if cert and os.access(cert, os.R_OK):
        if shutil.which("openssl", path=ctx.environ.get("PATH")):
            expiry = run_capture(["openssl", "x509", "-in", cert, "-noout", "-checkend", "86400"], os.getcwd(),
                                 ctx.environ, ctx.log, timeout=30)
            checks.append(make("rbe-tls-expiry", expiry.returncode == 0, "The TLS certificate is valid for 24+ hours.",
                               "The TLS certificate expires within 24 hours, is expired, or is unreadable."))
        else:
            checks.append(make_check("rbe-tls-expiry", WARNING, "openssl is not available; certificate expiry unchecked.",
                                     scope, required=False, affects=affects))
    cache = env.get("siso_cache_dir")
    checks.append(make("rbe-siso-cache", bool(cache) and os.path.isdir(cache),
                       "Siso cache directory exists: %s" % cache, "Siso cache directory is missing: %s" % cache,
                       [repair(["mkdir", "-p", cache])] if cache else [], cache_dir=cache))
    if enabled:
        checks.extend(_sync_artifacts(identity, scope, required, env, sync))
    return checks


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _sync_artifacts(identity, scope, required, env, sync):
    service, cache = env["rbe_service"], env.get("siso_cache_dir", "")
    siso_dir = identity.src / "build" / "config" / "siso"
    gclient, sisorc, sisoenv = (_read(identity.workspace / ".gclient"), _read(siso_dir / ".sisorc"),
                                _read(siso_dir / ".sisoenv"))

    def make(name, ok, good, bad):
        return make_check(name, PASS if ok else (BLOCKER if required else WARNING), good if ok else bad, scope,
                          required=required, affects=("rbe build",), repairs=[] if ok else sync)

    checks = [
        make("rbe-gclient", bool(gclient) and "reapi_address" in gclient and service in gclient,
             ".gclient names the RBE service.", ".gclient does not name the RBE service (sync has not applied RBE)."),
        make("rbe-sisorc", bool(sisorc) and "reapi_keep_exec_stream" in sisorc and "googlechrome" in sisorc
             and "-local_cache_enable" in sisorc and '-cache_dir "%s"' % cache in sisorc,
             ".sisorc has the RBE flags and local cache.", ".sisorc is missing RBE flags or the Siso cache (stale sync)."),
        make("rbe-sisoenv", bool(sisoenv) and service in sisoenv, ".sisoenv points at the RBE service.",
             ".sisoenv does not reference the RBE service (hooks may be stale)."),
    ]
    stale = []
    for generated in sorted((identity.src / "out").glob("*/args_generated.gni")):
        if generated.parent.name == "redirect_cc":
            continue
        text = _read(generated) or ""
        if not re.search(r"use_remoteexec\s*=\s*true", text):
            stale.append(generated.parent.name)
    checks.append(make_check(
        "rbe-gn-outputs", WARNING if stale else PASS,
        "Output directories not generated for RBE: %s. A forced GN regeneration at build time fixes this." % ", ".join(stale)
        if stale else "No discovered output directory contradicts the RBE configuration.",
        scope, required=False, affects=("rbe build",), stale=stale))
    return checks


def _reachability(scope):
    return make_check("rbe-reachability", NOT_CHECKED,
                      "Network and VPN reachability are not tested. Confirm the internal VPN yourself.",
                      scope, required=False, affects=("rbe build",))


def mac_build_checks(ctx, scope, remote_required=False):
    """Host and configuration checks for a macOS build; remote-build configuration is required when asked."""
    checks = _machine(ctx, scope)
    identity, error = _selection(ctx)
    checks.append(_disk(scope, identity.src if identity and identity.src.is_dir() else ctx.scaffold_root))
    if identity is None:
        return checks + _unchecked(("services-key", "rbe-config"), scope, error, False, ("mac build",))
    return checks + [_services_key(identity, scope)] + _rbe_config(ctx, identity, scope, required=remote_required) \
        + [_reachability(scope)]


def rbe_checks(ctx, scope):
    checks = [_reachability(scope)]
    if not shutil.which("openssl", path=ctx.environ.get("PATH")):
        checks.append(make_check("openssl", WARNING, "openssl is not on PATH; certificate expiry cannot be checked.",
                                 scope, required=False, affects=("rbe build",)))
    identity, error = _selection(ctx)
    if identity is None:
        return checks + _unchecked(("rbe-config",), scope, error, True, ("rbe build",))
    return checks + _rbe_config(ctx, identity, scope, required=True)
