# macOS

Existing-checkout workflows for Brave macOS on an arm64 Mac: Debug builds, test suites, and restarting the browser. Each command delegates to Core's own package scripts through the checkout-local Node and package manager; nothing here changes how Core builds. Verified support is listed by `bcore capabilities`; other configurations and architectures are accepted but only `limited` or `unverified`.

Prerequisites: a registered checkout with an approved environment ([getting started](getting-started.md)), `bcore doctor mac --checkout <name>` passing, and, for the default remote compilation, RBE/Siso configuration (see [readiness](#readiness-and-rbesiso-prerequisites)). Use one operator per checkout; stop other builds first.

## Build

```sh
bcore build                      # Debug arm64 with RBE/Siso
bcore build --offline            # compile locally instead
bcore build --standalone         # an app you can copy to another Apple Silicon Mac
bcore build --plan               # show the steps; runs nothing
bcore build -C Custom            # forwarded: output goes to <src>/out/Custom
```

`bcore build` checks readiness, applies Core patches only when they are out of date and no local edits are at risk ([source and cleanup](source-and-cleanup.md)), runs `bpm run build` with generated arguments, then verifies the application bundle in the resolved output directory and records it. It never cleans, installs, or launches anything.

Generated arguments, in order: `--target_os=mac --target_arch=arm64 -C Debug_arm64 Debug`, then `--use_remoteexec=true` (or `--offline`), plus `--channel=release` for Release and `--force_gn_gen` after `--force-gn` or a patch application. Everything else on the command line is forwarded unchanged after them, so new options of the package command work without any change here; use `--` to forward a token that is also a scaffold option (`bcore build -- --json`).

Forwarded options that decide what is built are also read for planning, output selection, and records: `--target_os`, `--target_arch`, `-C`, a `Debug`/`Release` build-configuration word, `--channel`, `--target`, `--offline`, and `--use_remoteexec`. A forwarded value replaces the generated one, so the child sees one choice; a repeated option follows the package command's parser (the last one wins). If a forwarded value contradicts an explicit scaffold selector, such as `--configuration release` with a forwarded `Debug`, the command stops before any change with both values and an example. A relative `-C` names a directory beneath Chromium's `src/out`, not Core: `-C Custom` selects `<src>/out/Custom`.

`--ninja C:<directory>` changes Ninja's directory after Core generates its build configuration. The scaffold forwards it but reports the artifact directory as unresolved, keeps other outputs' receipts unchanged, and cannot restart from that build. `--ninja f:<file>` selects a different build file: the artifact contract is unresolved and the selected directory's earlier output needs revalidation. Both `--ninja=<key>:<value>` and `--ninja <key>:<value>` forms have this behavior.

### Build results

| Outcome | Result |
| --- | --- |
| Child exits nonzero | `CHILD_FAILED`, exit 5, `child_exit_code` set |
| Child succeeds and the application is found in the resolved output | `ok`, the application listed in `artifacts` |
| Child succeeds but the output cannot be identified (for example `--target brave_unit_tests` builds no application, or `--prepare_only`, `--xcode_gen`, and non-building Ninja options such as `--ninja n:` (a dry run) or `--ninja t:<tool>` exit without compiling: an application already at the expected path is not treated as this build's output) | `build`, `sync-build`: `ok` with an `ARTIFACT_UNRESOLVED` warning and no artifact. `build-run`, `sync-build-run`: `ARTIFACT_UNRESOLVED`, exit 5, nothing stopped or launched |
| Output identified but missing or unusable | `ARTIFACT_MISSING` or `ARTIFACT_MISMATCH`, exit 5 |

Nothing falls back to another artifact. A build that exits without compiling keeps the earlier build record for that output unchanged (`--prepare_only`, and Ninja dry runs, help, and version); modes that may write into the output directory (`--xcode_gen`, `--ninja t:<tool>` such as `clean`) mark it as needing revalidation. A forwarded `--gn target_os:<value>` or `--gn target_cpu:<value>` that differs from the build's target or architecture also leaves the output identity unresolved, because the build is then not the one its output directory and record describe; against an explicit target argument it is a `SELECTOR_CONFLICT`.

## Standalone builds for another Mac

```sh
bcore build mac --standalone
bcore build-run mac --standalone
bcore run mac --standalone
```

A Debug build is a component build by default. Its app loads several hundred libraries from the output directory through `@rpath` entries such as `@loader_path/../../../../../..`, so a copied `.app` stops at launch with `dyld` errors. `--standalone` adds the generated argument `--gn=is_component_build:false`, after Core's own defaults, so those libraries are linked into the app bundle. `build`, `build-run`, `sync-build`, and `sync-build-run` accept it. Forwarding `-- --gn=is_component_build:false` yourself builds the same app, but in the normal output directory, without the bundle check, and recorded as an ordinary build that `bcore run mac --standalone` does not select.

- **Output directory.** A standalone Debug build has its own directory, `out/Debug_standalone_arm64`, so component builds and `bcore test mac`, which use `out/Debug_arm64`, never switch it back. Release is already a non-component build, so `--standalone --configuration release` keeps `out/Release_arm64` and adds only the bundle check. A forwarded `-C` still wins. A second output costs as much disk as the first; to reuse the component directory instead, forward `-C Debug_arm64`.
- **Switching an existing directory.** Changing a directory between component and standalone builds rebuilds almost everything in it. Before the build starts, the console shows a `Full rebuild:` line when the directory's `args_generated.gni` records the other mode, and `--plan` shows the same notice on its build step. A line you add to `args.gn` after its import wins over what Core generates, so no notice appears for it, and `--standalone` against an `args.gn` that sets `is_component_build = true` is a `SELECTOR_CONFLICT` before anything runs. As with every build, the directory's previous success record is marked as needing revalidation before the package command starts; it stays as history.
- **Running it.** `bcore run mac` selects arm64 builds and runs the component one; for another architecture, pass `--artifact`. `bcore run mac --standalone` runs a standalone build that passed the bundle check, found from its build record; if several match, choose one with `--artifact` instead (the two options cannot be combined). Each output directory's build record decides its mode, so after `--standalone -C Debug_arm64`, plain `bcore run mac` does not pick that app.
- **Conflicts.** A forwarded `--gn=is_component_build:true` with `--standalone` is a `SELECTOR_CONFLICT`, and nothing runs. A forwarded `--gn=is_component_build:false` is accepted and not repeated.
- **Other targets.** Android and iOS builds are already non-component builds, so `--standalone` with `android`, `ios`, or a forwarded `--target_os=android` is rejected with `INVALID_INPUT`, for `run` as well as the build commands.
- **Verification.** After the build, output verification reads the load commands (`LC_LOAD_DYLIB` and related, and `LC_RPATH`) of every Mach-O file in the bundle. If a library reference other than a system library under `/System/` or `/usr/lib/` resolves outside the bundle, or resolves nowhere (a weakly linked library may be missing, because dyld skips it), the build fails with `ARTIFACT_MISMATCH` and lists the references; `build-run` then launches nothing. `@rpath` references are searched with the run paths of the file that loads them and of the bundle's executables, which approximates `dyld`. A verified standalone artifact has `"standalone": true`.

The ad-hoc signature the linker adds is not tied to one Mac. Moving the app:

1. Archive it with `ditto`, which keeps the bundle's symbolic links, permissions, and extended attributes; plain `zip` stores copies in place of the framework's symbolic links, which breaks the bundle's signature: `ditto -c -k --keepParent "out/Debug_standalone_arm64/Brave Browser Development.app" BraveDevelopment.zip`.
2. On the other Mac, unpack it with `ditto -x -k BraveDevelopment.zip .` or Finder.
3. If the archive arrived through a browser, AirDrop, or a chat app, clear the quarantine attribute first: `xattr -dr com.apple.quarantine "Brave Browser Development.app"`.
4. If you change anything inside the bundle, re-sign it ad hoc: `codesign --force --deep -s - "Brave Browser Development.app"`.

The default build is arm64, so the receiving Mac must be Apple Silicon. A build for another architecture, such as `-- --target_arch=x64`, is checked the same way but runs with `--artifact`.

## Test

```sh
bcore test brave_unit_tests
bcore test mac brave_browser_tests --filter 'Example.*'
bcore test brave_browser_tests -- --gtest_repeat=2
bcore test mac --plan
bcore test --file components/example/example_unittest.cc
```

`bcore test` with no suite runs the changed macOS tests, and `--file` runs one file's tests; see [test](commands.md#test). To run a suite, name it. The suite comes first; `mac` is optional and only recognized before the suite. `--filter` narrows tests inside the suite and never supplies a missing suite. Both `--filter 'Example.*'` and `--filter='Example.*'` pass the pattern unchanged; colon-separated patterns select multiple groups in the same suite. Core's test command builds the selected suite before running it.

Tests use the selected checkout's approved environment and checkout-local tools. They default to Debug arm64 with remote execution requested. `--configuration`, `--offline`, and forwarded output options such as `-C` select the effective build just as they do for `bcore build`. Build and test output streams live to the console and the diagnostic log. A failed package command returns scaffold exit 5 and saves the child's status as `child_exit_code` in JSON and the operation record. Other arguments go to `bpm run test` after the generated ones.

Desktop suites also pass `--test-launcher-summary-output=<output directory>/scaffold_test_results.json` to the test binary (unless you forward that option yourself, or the output directory is unresolved) and read the launcher's own counts afterward. A run that exits 0 but ran no tests fails with `NO_TESTS_RAN`, so a filter that matches nothing is no longer reported as a pass; a parameterized fixture needs an instantiation prefix, such as `--filter '*/Fixture.*'`. A missing or unreadable summary leaves the run successful with a `TEST_RESULTS_UNVERIFIED` warning.

A test run that fails or is interrupted, including one that exits 0 but whose results show failed tests or none run, marks the output directory as needing revalidation, shown by `bcore status` and by a warning from `bcore run`. The test command compiles into the same directory as the application, and the scaffold cannot tell a compile failure from a failing test, so it assumes the output may be partly overwritten. A passing test run does not clear the mark and does not add one; only a build that verifies the output clears it.

Android tests are covered in [Android](android.md#tests). The effective target decides whether `test` runs the macOS or the Android path: an Android default platform or forwarded `--target_os=android` selects Android, forwarding `--target_os=mac` overrides an Android default, and `bcore test mac <suite> --target_os=android` is a `SELECTOR_CONFLICT`. `--device` applies to Android only.

## Run and restart

```sh
bcore run                        # restart with the default Debug output
bcore run --artifact ./out/Custom/'Brave Browser Development.app'
bcore build-run                  # build, then run exactly what was built (alias: br)
```

`run` never builds. It selects the application by checkout, target, configuration, and architecture; if more than one valid application matches, pass `--artifact`. Then it quits every running instance of the same application (matched by bundle identifier, including one from another checkout), waits for exit, escalating from a graceful quit to termination to a forced kill, launches the selected bundle, and confirms its process appeared. Other applications and unrelated processes are left alone, profiles and app data are kept, and a failed preflight leaves the running browser untouched. If the process list cannot be read completely (the listing fails, times out, or is cut off), or a liveness check fails while confirming that an instance exited, the restart stops with `LAUNCH_FAILED` instead of treating it as no instance running; an empty successful listing is the only proof that nothing is running.

An older or independently built output may run. `run` checks the selected application and restarts it without comparing source files, dependency repositories, or build records. It does not report build freshness or rebuild anything. Failed or interrupted builds still mark their output records as needing revalidation; launching an application does not clear that state, and `run` warns (`ARTIFACT_FRESHNESS_UNKNOWN`) before it launches an output with that mark.

`sync-build` and `sync-build-run` (aliases `sb`, `sbr`) run the sync phase first and send extra arguments to the build phase only; use `--sync-arg` for Core's sync (see [commands](commands.md#forwarding-to-package-commands)). See [source and cleanup](source-and-cleanup.md) for sync.

## Verification status

Checked on a real macOS arm64 checkout on 2026-09-30: environment loading and checkout-local tools, a Debug build through RBE/Siso (the output was already up to date, so no compilation ran), one browser test, restart of the application including quitting a running instance, and drift inspection. Not yet verified on a real checkout: sync, patch update, cleanup, compiling after source changes, and offline builds. `bcore capabilities` reflects this.

## Readiness and RBE/Siso prerequisites

`bcore doctor mac` and `bcore doctor rbe` report these checks without changing anything. Nothing is installed, repaired, synced, or approved, and nothing is written to the checkout.

### macOS build readiness (`doctor mac`)

| Check | Verifies | Required |
| --- | --- | --- |
| `macos-sdk` | `xcrun --show-sdk-version` reports an SDK | yes |
| `metal-toolchain` | `xcrun metal` works, or a Metal toolchain component is mounted | no (warning) |
| `disk-space` | At least 150 GiB free where the checkout lives | no (warning) |
| `services-key` | `brave_services_key` is nonempty in Core's `.env` (including `include_env=` files). The value is never shown and its validity is not verified | yes (error) |
| `rbe-*` | The RBE checks below. In `doctor mac` they are warnings, because doctor does not know how you will compile | no |

Building is stricter than doctor about the compile mode. `build`, `test`, and the combined commands treat the local RBE configuration checks as required when the effective mode is remote (the default) and not at all when it is local (`--offline`, or a forwarded `--use_remoteexec=false`); a missing key, unreadable certificate, or absent Siso cache directory then stops the command with `READINESS_BLOCKED` before anything is prepared. Reachability of the service is never tested. Readiness is also checked per phase: `sync` needs the host but not the build's target or the RBE artifacts that sync refreshes, and `sync-build` checks the build's readiness and tools again after the sync step.

The Xcode developer directory is checked separately by `host-macos-arm64` and `xcode-developer-directory`. Node, the package manager, and `vpython3` are checked by `local-tools`, from the checkout's own payloads.

Checks that need a checkout report `not_checked` when none is selected; the machine checks still run.

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

In scope `rbe`, problems with these are required blockers. In scope `mac` they are warnings, because compiling locally with `--offline` needs no RBE. Use `--offline` on the build command to choose local compilation explicitly; the tools never fall back to it silently.

### Network limits

Doctor does not test the internal VPN, the RBE service, or authentication with it. A passing result means the local configuration looks complete, not that a remote build will succeed. Confirm the VPN yourself before an RBE build or sync. Service addresses that contain credentials are redacted in results.
