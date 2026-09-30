# macOS

Existing-checkout workflows for Brave macOS on an arm64 Mac: Debug builds, test
suites, and restarting the browser. Each command delegates to Core's own package
scripts through the checkout-local Node and package manager; nothing here changes
how Core builds. Verified support is listed by `bdev capabilities`; other
configurations and architectures are accepted but only `limited` or `unverified`.

Prerequisites: a registered checkout with an approved environment
([getting started](getting-started.md)), `bdev doctor mac --checkout <name>`
passing, and, for the default remote compilation, RBE/Siso configuration (see
[readiness](#readiness-and-rbesiso-prerequisites)). Use one operator per checkout;
stop other builds first.

## Build

```sh
bdev build                      # Debug arm64 with RBE/Siso
bdev build --offline            # compile locally instead
bdev build --plan               # show the steps; runs nothing
bdev build -C Custom            # forwarded: output goes to <src>/out/Custom
```

`bdev build` checks readiness, applies Core patches only when they are out of date
and no local edits are at risk ([source and cleanup](source-and-cleanup.md)), runs
`bpm run build` with generated arguments, then verifies the application bundle in
the resolved output directory and records it. It never cleans, installs, or
launches anything.

Generated arguments, in order: `--target_os=mac --target_arch=arm64 -C Debug_arm64
Debug`, then `--use_remoteexec=true` (or `--offline`), plus `--channel=release` for
Release and `--force_gn_gen` after `--force-gn` or a patch application. Everything
else on the command line is forwarded unchanged after them, so new options of the
package command work without any change here; use `--` to forward a token that is
also a scaffold option (`bdev build -- --json`).

Forwarded options that decide what is built are also read for planning, output
selection, and records: `--target_os`, `--target_arch`, `-C`, a `Debug`/`Release`
build-configuration word, `--channel`, `--target`, `--offline`, and
`--use_remoteexec`. A forwarded value replaces the generated one, so the child sees
one choice; a repeated option follows the package command's parser (the last one
wins). If a forwarded value contradicts an explicit scaffold selector, such as
`--configuration release` with a forwarded `Debug`, the command stops before any
change with both values and an example. A relative `-C` names a directory beneath
Chromium's `src/out`, not Core: `-C Custom` selects `<src>/out/Custom`.

### Build results

| Outcome | Result |
| --- | --- |
| Child exits nonzero | `CHILD_FAILED`, exit 5, `child_exit_code` set |
| Child succeeds and the application is found in the resolved output | `ok`, the application listed in `artifacts` |
| Child succeeds but the output cannot be identified (for example `--target brave_unit_tests` builds no application, or `--prepare_only`, `--xcode_gen`, and non-building Ninja options such as `--ninja n:` (a dry run) or `--ninja t:<tool>` exit without compiling: an application already at the expected path is not treated as this build's output) | `build`, `sync-build`: `ok` with an `ARTIFACT_UNRESOLVED` warning and no artifact. `build-run`, `sync-build-run`: `ARTIFACT_UNRESOLVED`, exit 5, nothing stopped or launched |
| Output identified but missing or unusable | `ARTIFACT_MISSING` or `ARTIFACT_MISMATCH`, exit 5 |

Nothing falls back to another artifact. A build that exits without compiling keeps the
earlier build record for that output unchanged (`--prepare_only`, and Ninja dry runs, help, and version);
modes that may write into the output directory (`--xcode_gen`, `--ninja t:<tool>` such as `clean`) mark it as
needing revalidation. A forwarded `--gn target_os:<value>` or `--gn target_cpu:<value>` that differs from the
build's target or architecture also leaves the output identity unresolved, because the build is then not the one
its output directory and record describe; against an explicit target argument it is a `SELECTOR_CONFLICT`.

## Test

```sh
bdev test brave_unit_tests
bdev test mac brave_browser_tests --filter 'Example.*'
bdev test brave_browser_tests -- --gtest_repeat=2
```

The suite is required and comes first; `mac` is optional and only recognized
before the suite. `--filter` narrows tests inside the suite and never supplies a
missing suite. Other arguments go to `bpm run test` after the generated ones.
Android tests are not available; asking for them (or omitting the target while
`defaults.platform` is `android`, or forwarding `--target_os=android`) fails with
`UNSUPPORTED_CAPABILITY` before anything is loaded or built. The effective target
decides: forwarding `--target_os=mac` overrides an Android default, while
`bdev test mac <suite> --target_os=android` is a `SELECTOR_CONFLICT`.

## Run and restart

```sh
bdev run                        # restart with the default Debug output
bdev run --artifact ./out/Custom/'Brave Browser Development.app'
bdev build-run                  # build, then run exactly what was built (alias: br)
```

`run` never builds. It selects the application by checkout, target, configuration,
and architecture; if more than one valid application matches, pass `--artifact`.
Then it quits every running instance of the same application (matched by bundle
identifier, including one from another checkout), waits for exit, escalating from a
graceful quit to termination to a forced kill, launches the selected bundle, and
confirms its process appeared. Other applications and unrelated processes are left
alone, profiles and app data are kept, and a failed preflight leaves the running
browser untouched.

An older or independently built output may run. Structured results carry
`freshness` (`current`, `stale`, or `unknown`) with evidence, and a warning says
when the output may not include the latest code: `STALE_BUILD`, or
`UNKNOWN_FRESHNESS` ("Build freshness is unknown; this output may not include the
latest code."). After a failed, cancelled, or interrupted rebuild the earlier
record is marked as needing revalidation, so freshness is reported as unknown even
if an older application is still usable; only a later successful, validated build
clears it (a passing `bdev test` run proves nothing about the application).

A successful build records these inputs, and a later `run` compares them: Core's and
Chromium's HEAD, Core's uncommitted state, the tracked Chromium files that differ from
HEAD (name, size, and modification time; no file is hashed), the stat signatures of
patched files, and Core's `.env` together with every file it pulls in with
`include_env`. For Android outputs it also compares the support working copy's
revision and uncommitted state and the copied resources. Any difference is `stale`.
An input that cannot be computed (for example Git does not answer within 90 seconds),
or a record written before an input was compared, makes the result `unknown`. A match
is reported as `current` with the limits stated in its evidence: untracked Chromium
files and edits inside repositories nested in the Chromium checkout (such as `v8`) are
not tracked, and nothing hashes the whole tree.

`sync-build` and `sync-build-run` (aliases `sb`, `sbr`) run the sync phase first and
send extra arguments to the build phase only. See
[source and cleanup](source-and-cleanup.md) for sync.

## Verification status

Checked on a real macOS arm64 checkout on 2026-09-30: environment loading and
checkout-local tools, a Debug build through RBE/Siso (the output was already up to
date, so no compilation ran), one browser test, restart of the application including
quitting a running instance, and drift inspection. Not yet verified on a real
checkout: sync, patch update, cleanup, compiling after source changes, and offline
builds. `bdev capabilities` reflects this.

## Readiness and RBE/Siso prerequisites

`bdev doctor mac` and `bdev doctor rbe` report these checks without changing
anything. Nothing is installed, repaired, synced, or approved, and nothing is
written to the checkout.

### macOS build readiness (`doctor mac`)

| Check | Verifies | Required |
| --- | --- | --- |
| `macos-sdk` | `xcrun --show-sdk-version` reports an SDK | yes |
| `metal-toolchain` | `xcrun metal` works, or a Metal toolchain component is mounted | no (warning) |
| `disk-space` | At least 150 GiB free where the checkout lives | no (warning) |
| `services-key` | `brave_services_key` is nonempty in Core's `.env` (including `include_env=` files). The value is never shown and its validity is not verified | no (warning) |
| `rbe-*` | The RBE checks below. In `doctor mac` they are warnings, because doctor does not know how you will compile | no |

Building is stricter than doctor about the compile mode. `build`, `test`, and the
combined commands treat the local RBE configuration checks as required when the
effective mode is remote (the default) and not at all when it is local (`--offline`, or a
forwarded `--use_remoteexec=false`); a missing key, unreadable certificate, or absent
Siso cache directory then stops the command with `READINESS_BLOCKED` before anything is
prepared. Reachability of the service is never tested. Readiness is also checked per
phase: `sync` needs the host but not the build's target or the RBE artifacts that sync
refreshes, and `sync-build` checks the build's readiness and tools again after the sync
step.

The Xcode developer directory is checked separately by `host-macos-arm64` and
`xcode-developer-directory`. Node, the package manager, and `vpython3` are
checked by `local-tools`, from the checkout's own payloads.

Checks that need a checkout report `not_checked` when none is selected; the
machine checks still run.

### RBE/Siso configuration (`doctor rbe`)

RBE is the default for desktop compilation. These checks read local files only.

| Check | Verifies |
| --- | --- |
| `rbe-env` | `.env` sets `rbe_service`, `rbe_tls_client_auth_cert`, `rbe_tls_client_auth_key`, `siso_cache_dir`, and `use_remoteexec=true` |
| `rbe-siso-mode` | Native Siso mode: `use_siso` is true (default) and `use_reclient` is false (default) |
| `rbe-tls-files` | The TLS certificate and key are readable. Contents are never read or shown |
| `rbe-tls-expiry` | The certificate is valid for at least 24 hours (needs `openssl`) |
| `rbe-siso-cache` | The Siso cache directory exists |
| `rbe-gclient`, `rbe-sisorc`, `rbe-sisoenv` | Sync outputs (`.gclient`, `.sisorc`, `.sisoenv`) reference the RBE service and cache. Stale outputs need a sync with the internal VPN connected |
| `rbe-gn-outputs` | Existing `out/*/args_generated.gni` files agree with RBE (warning only; a forced GN regeneration at build time fixes it) |
| `rbe-reachability` | Always `not_checked` |

In scope `rbe`, problems with these are required blockers. In scope `mac` they
are warnings, because compiling locally with `--offline` needs no RBE. Use
`--offline` on the build command to choose local compilation explicitly; the
tools never fall back to it silently.

### Network limits

Doctor does not test the internal VPN, the RBE service, or authentication with
it. A passing result means the local configuration looks complete, not that a
remote build will succeed. Confirm the VPN yourself before an RBE build or sync.
Service addresses that contain credentials are redacted in results.
