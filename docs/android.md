# Android

Existing-checkout Android workflows on an Apple Silicon Mac: a Debug arm64 APK
build, installing and restarting it on a device, and running Robolectric/JUnit and
device-backed Java tests. Android Studio project generation and emulator launching
are not available. Verified support is
listed by `bdev capabilities`.

Prerequisites: a registered checkout with an approved environment
([getting started](getting-started.md)), the platform tools (`adb`), and the
checkout's Android support working copy (below). `bdev doctor android --checkout
<name>` reports each of them without changing anything.

## One-time setup per checkout

1. Add Android to the checkout's targets: `bdev sync android`. This builds
   `--target_os` from the union of the checkout's existing `.gclient` values and
   `android`, and changes the checkout ([source and cleanup](source-and-cleanup.md)).
   `bdev sync-build android` does this first and then checks the build's readiness
   (the Android target, and the local RBE configuration unless you compile with
   `--offline`) again; a standalone `bdev build android` needs the target already.
2. Create the shared support checkout and workspace link: `bdev android setup`. This is
   the step that fetches support resources.

## Android-on-Mac support repository

Compiling Android from macOS needs resources and patches Chromium does not ship,
kept in a separate support repository. Setup defaults to its `android-testing-prototype`
branch. One shared checkout lives at `brave-android-mac-support` beside the scaffold's
configuration file. Each browser workspace links to it at
`<workspace>/brave-android-mac-support`. All linked checkouts use the same revision;
changing it affects each checkout. There is no separate bare Git cache or shared
LFS store: large-file objects belong to the shared checkout.

This dependency is optional and only serves Android builds on macOS. Scaffold
setup, shell activation, macOS/iOS builds, and other host platforms do not install
or require it. The scaffold's current Android build support remains macOS arm64.

Set `android_support_path` in local `brave-scaffold.toml` to choose another location.
Relative paths are resolved from that file; absolute paths also work. For example,
`android_support_path = "dependencies/brave-android-mac-support"` belongs at the top
level, before any table headers.

Explicit setup adopts an existing workspace copy when the shared location is
empty and on the same filesystem. Otherwise, it preserves a real workspace copy as
`brave-android-mac-support.previous` before creating the link. It stops if that
backup already exists or the workspace links to another location. Existing local
changes and commits stay in the adopted or preserved checkout. Old caches are
left for separate cleanup. Do not run setup or switch the shared revision while
another checkout is building. Concurrent builds have not been verified.

`bdev android setup` is also the only command that fetches large files. It materializes content from
the checkout’s LFS store, fetches whatever is missing from the source, and then lists the
large files that are still pointers: if any remain, it fails with `CHILD_FAILED` (a
plain `git lfs checkout` reports success while leaving pointers), and it verifies an
existing working copy the same way when you run it again after an interruption. `bdev
doctor android` (`android-support-lfs`) and builds only look: pointers are a blocker
or `DEPENDENCY_INCOMPATIBLE` that names `bdev android setup`, and nothing is fetched.

```sh
bdev android setup                     # clone at the default ref
bdev android setup --ref <tag-or-sha>  # change the revision for all linked checkouts
bdev android setup --source <url-or-path>
```

The scaffold never resets or cleans an existing working copy. `--ref`
switches the shared checkout only when it has no local changes and no unpushed commits; otherwise
the command stops with `PREPARATION_CONFLICT` and lists what it found. Any branch
or commits you keep there are used as they are.

### Compatibility

A stored revision hash proves neither compatibility nor reproducibility, so none
is used. The support repository carries its own version gates (Chromium and
toolchain versions); `copyMacRes.sh -v` checks them against the checkout without
copying anything. A build runs that gate first. If it fails, the command stops
with `DEPENDENCY_INCOMPATIBLE`, the gate's own reason, and repairs (list the
revisions, then `bdev android setup --ref <ref>`). What is verified: the gate result
for this checkout at build time. What is not: that an untested combination builds
successfully, or that the gate covers every incompatibility. Sources live in
`scripts/src/scaffold/brave/android_support.toml`.

### What preparation changes

Before compiling, a build compares the working copy with the checkout and runs only
the support scripts that are needed: `applyPatches.sh` when support patches are not
applied (`applyPatches.sh -v` fails) or the working copy's inputs changed, and
`copyMacRes.sh` when resources are not copied or stale. Either runs when there is no
record of a refresh for this checkout (`.bdev/state/`). The scripts change Chromium
files (in the source root and in nested repositories such as `v8`) and copy resources
into the checkout's `third_party` directories (the paths `copyMacRes.sh` declares);
GN files are then regenerated.

Before executing either script, including its `-v` checks, the scaffold compares
its SHA-256 with the reviewed identities in the tooling installation. Each identity
has a complete manifest of patch repositories, patch files, direct source edits,
and resource destinations. A support repository cannot approve its own code.
Unknown or changed shell code stops with `PREPARATION_CONFLICT` before execution;
select a supported implementation or extend and review the tooling adapter first.

Patch data can change within a supported script contract. The guard reads every
current patch target in the repository the manifest names and checks direct source
writes too. Missing or unreadable repository discovery blocks preparation.
Resource copying and signing stay within the declared destinations. Plans and execution
use this same inventory. Missing repository discovery, unreadable patch formats, and
unknown scripts block preparation before the support scripts change files.

A build attempts refresh after its checks pass when patches or resources are stale,
support inputs change, or the checkout has no matching refresh record. A later
script or write-scope check can still stop refresh. It does not prompt or back up files.
`applyPatches.sh` resets its patch targets and applies support patches, and edits the
direct source paths its manifest lists. `copyMacRes.sh` copies and replaces resources
and may sign files within its declared directories. Local edits in those paths may be
lost, including files left by an earlier refresh or supplied by Chromium. The scripts
control replacement; Scaffold does not reset the Git index or remove extra files itself.
Keep wanted edits elsewhere before running a build that needs refresh.

Use `bdev build android --skip-support-refresh` to stop if refresh is needed. This option
also applies to `build-run`, `sync-build`, and `sync-build-run`; it does not suppress their
other preparation or sync steps. Current support needs no refresh and builds normally.
`doctor android` uses the same preparation decision: current support passes, a needed
refresh warns, and script, compatibility, or write-inventory errors block readiness.
A differing resource with no matching saved copy record has an unknown origin;
that difference alone does not prove local edits. Doctor shows "Android support files";
JSON keeps the check name `android-support-currency`. Doctor never refreshes support.
Sync runs Core's own command and can overwrite local changes; see
[sync behavior](source-and-cleanup.md#sync-sources).

After execution, a tracked change outside the declared scope is an adapter failure:
the build stops, records nothing as prepared, and leaves the files for review.

The build also keeps a marked block of GN overrides (no component build, no
secondary ABI, `use_mold=false`, `android_static_analysis="off"`, remote execution)
at the end of the output's `args.gn`, after the import of Core's generated arguments,
so the block wins over them. A setting you forward for that build (`--gn=<key>:<value>`
or `--use_remoteexec=...`) is left out of the block and takes effect; the next build
without it returns to the defaults.
Everything outside the block is left alone.

## Build

```sh
bdev build android                  # Debug arm64 APK
bdev build android --offline        # compile locally
bdev build android -C Custom        # forwarded: output in <src>/out/Custom
```

The package build script receives `--target_os=android --target_arch=arm64
--target_android_output_format=apk -C android_Debug_arm64 Debug`, `--gn=` values
for the overrides above, and `--use_remoteexec=true` (or `--offline`), then your
forwarded arguments; a forwarded value replaces the matching generated one. Child
environment adjustments: Android preference-home variables are unset, `JAVA_OPTS`
defaults to `-Xmx10G -Xms1G`, and Siso's local job limit defaults to 8 (set
`SCAFFOLD_ANDROID_SISO_LOCAL_JOBS` or `SISO_LIMITS=local=N`). Rules for forwarding,
output selection, and artifact outcomes are the same as on
[macOS](macos.md#build). The verified output is
`<src>/out/android_Debug_arm64/apks/BraveMonoarm64.apk`; a zip without
`AndroidManifest.xml`, or a package that is not `com.brave.*`, is `ARTIFACT_MISMATCH`.
The package name is read from the APK with the checkout's own `aapt2`. If that tool is
missing or fails, the package is unproven: the result is `ARTIFACT_UNRESOLVED` (a warning
for `build`, an error for the combined commands and `run`), and nothing is installed,
stopped, or launched. No default package name is assumed.

## Tests

```sh
# Host-side Robolectric/JUnit: runs on this Mac, no device
bdev test android brave_junit_tests --filter='*BraveCommandLineInitUtilTest*'

# Instrumented tests: run on the selected emulator or device
bdev test android brave_java_unit_tests --filter='BraveAppearancePreferencesTest.*' --device=emulator-5554
```

Two suites are available. `brave_junit_tests` needs no device and rejects `--device`.
`brave_java_unit_tests` runs on one device chosen as for `run` (`--device`, then
`defaults.android_device`, then the only usable device; `DEVICE_AMBIGUOUS` and
`DEVICE_UNAVAILABLE` apply) before anything is built. `--device` is a scaffold
option: the scaffold passes the selected device and `adb` path to the test command
itself, and forwarding `--device`, `-s`, or `--adb-path` is a `SELECTOR_CONFLICT`.
Other suites fail with `UNSUPPORTED_CAPABILITY`.

`--filter` reaches the suite's runner as a gtest-style filter. For
`brave_junit_tests` use a fully qualified class (`org.example.SomeTest.*`) or a
wildcard (`*SomeTest*`); a bare `SomeTest.*` can match no tests.

Device-backed tests also accept `--all-devices`, and the terminal picker offers
`a` for All. The suite builds once, then runs on each usable device with a
compatible ABI. A failed run does not stop the remaining devices. Each device
gets its own results file and entry in `data.devices`; any failed run makes the
command return a nonzero exit. `test-local --all-devices` uses the same selection
for device suites and still runs host suites on this Mac. Host-only `test`
commands reject `--all-devices`. It cannot be combined with `--device`.
If you forward `--json-results-file`, its filename receives a distinct suffix
for each device so results are not overwritten. Plans never prompt or run tests.

**Required support branch.** The checkout's support working copy must be on the
`android-testing-prototype` branch. This is checked before anything is prepared or
built. Another branch or a detached HEAD fails with `DEPENDENCY_INCOMPATIBLE`. The
scaffold never switches the repository; run `git -C <working copy> switch
android-testing-prototype` yourself.

**Core overlay.** Running an Android test is an explicit request that changes Core:
the support repository's `applyBraveCoreTestSupport.sh` applies its test overlay to
three files under Core's `build/commands` (`lib/androidTestMacHost.ts`,
`lib/androidTestMacHost.test.ts`, `scripts/test.ts`). The script's identity and write
list are reviewed, and a patch that writes elsewhere is refused. An overlay that is
already applied is left alone; a partial or conflicting one is a
`PREPARATION_CONFLICT` and is never forced. The overlay stays applied afterwards.
Nothing else in Core changes, and `bdev setup` and `env init` never apply it. To
remove it, run `./applyBraveCoreTestSupport.sh --src-root <src> --reverse` from the
support working copy. A sync checks for local work and may stop on the applied
overlay, so reverse it first if it does.

**Preparation and build.** Patches and support files are prepared as for `build`,
then the overlay. Tests build in `<src>/out/android_tests_Debug_arm64`, so an app
build's output is not changed; `-C` selects another directory. Remote execution is
requested by default; `--offline` compiles locally. `--plan` shows the steps,
including the overlay and device choice, without changing anything.

**Results.** The scaffold asks the runner for a JSON results file,
`scaffold_test_results.json` in the output directory (unless you forward your own
`--json-results-file`). A nonzero runner exit is `CHILD_FAILED` with the child's
status and the result counts when readable. After a zero exit:
`TEST_FAILED` if any recorded test failed, `NO_TESTS_RAN` if none ran (check the
filter), and a `TEST_RESULTS_UNVERIFIED` warning if the file is missing. A missing
runner script is `ARTIFACT_MISSING`. Success reports passed and skipped counts.

## Install and restart

```sh
bdev run android --device <id>
bdev deploy android                 # same as 'run android'
bdev build-run android --device <id>
bdev build-run android --all-devices
bdev run android --all-devices
```

Device selection: `--device`, otherwise `defaults.android_device` from your
configuration, otherwise the only usable device. With several usable devices,
an interactive terminal shows a numbered picker with names, ids, and physical
device or emulator labels. You can save the choice in `defaults.android_device`
after selecting a device by number; Enter selects the first listed device.
For install/run commands, `a` selects All compatible devices, with the same
checks and results as `--all-devices`. All is used for this command only.
The default is saved in the active scaffold configuration; declining uses the
selected device for this command only.
The picker runs before building. JSON output, noninteractive commands, and
`--plan` never prompt: they report `DEVICE_AMBIGUOUS` with a copyable `--device`
option for each usable device. A device that is `offline` or `unauthorized`, or not
connected, gives `DEVICE_UNAVAILABLE` with recovery steps. `build-run` chooses the
device before building.

`--all-devices` applies to `run`, `deploy`, `build-run`, and `sync-build-run` on
Android. It overrides the saved default and cannot be combined with `--device`.
Combined commands build once. Before building, the scaffold checks each usable
device's supported ABIs against the effective build architecture. Before installing,
it reads the actual APK's ABIs and minimum Android API version and checks them
against each device. The minimum API version is checked after the build because
it comes from the finished APK. Unknown APK requirements stop deployment.
Offline, unauthorized, incompatible, or unreadable devices are skipped with a
reason. If none can be used, the command fails. Deployment continues after a
device fails, reports every device's result, and returns a nonzero exit if any
deployment fails. JSON keeps these results in `data.run.devices`; operation
records retain them too. `--plan` lists the devices and compatibility checks
without installing, building, prompting, or saving a default.

The APK is installed over the existing app (`adb install -d -r -g`), the APK's own
package is stopped on that device only (a failed stop is `LAUNCH_FAILED`), launched, and
its process confirmed. Each install, stop, and launch phase starts immediately
before its command. Failed steps keep the observed child exit; later steps remain
unattempted. Launch success is recorded only after PID confirmation, with the
launch command exit and verification exit recorded separately. Profiles and
app data are kept: nothing is uninstalled or cleared. Older or independently built
APKs may be installed. `run` and `deploy` check the APK and device, then install and
launch; they do not scan sources or compare build freshness. `adb` comes from `ADB`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, or `PATH`.

## Recovery and limits

- Failed, cancelled, or interrupted builds mark the output for revalidation; an
  older valid APK can still be installed. Nothing rebuilds or deletes for you.
- Clean outputs with `bdev clean android` ([source and cleanup](source-and-cleanup.md)).
- Release, other architectures, and AAB output are accepted but `limited`.
- Checked on a real checkout and emulator on 2026-09-30: creating the support
  working copy from a local clone, the compatibility gate, support preparation, a
  Debug arm64 build through RBE/Siso, and deploying to the only connected device
  without naming it. Not yet verified: `bdev sync android`, cleanup, a second
  checkout at a different support revision, and physical devices.

`android setup --json` uses result schema version 2. Its data reports
`shared_checkout`, `workspace_link`, and `preserved_copy` instead of bare-cache
fields. Other commands keep result schema version 1.
