# Android

Existing-checkout Android workflows on an Apple Silicon Mac: a Debug arm64 APK
build, and installing and restarting it on a device. Android tests, Android Studio
project generation, and emulator launching are not available. Verified support is
listed by `bdev capabilities`.

Prerequisites: a registered checkout with an approved environment
([getting started](getting-started.md)), the platform tools (`adb`), and the
checkout's Android support working copy (below). `bdev doctor android --checkout
<name>` reports each of them without changing anything.

## One-time setup per checkout

1. Add Android to the checkout's targets: `bdev sync android`. This builds
   `--target_os` from the union of the checkout's existing `.gclient` values and
   `android`, and changes the checkout ([source and cleanup](source-and-cleanup.md)).
2. Create the checkout's own support working copy: `bdev android setup`. This is
   the only step that uses the network.

## Android-on-Mac support repository

Compiling Android from macOS needs resources and patches Chromium does not ship,
kept in a separate support repository. Each checkout gets its own working copy
at `<workspace>/brave-android-mac-support`, beside (not inside) its sources. Two
checkouts that need different revisions therefore never switch a shared tree. Only
the Git object cache, `.bdev/cache/android-support.git` next to your
configuration, is shared.

```sh
bdev android setup                     # clone at the default ref
bdev android setup --ref <tag-or-sha>  # use a revision for this checkout only
bdev android setup --source <url-or-path>
```

The scaffold never resets, cleans, or switches an existing working copy. `--ref`
switches one only when it has no local changes and no unpushed commits; otherwise
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

Before compiling, a build compares the working copy with the checkout and refreshes
only when needed: support patches not applied (`applyPatches.sh -v` fails),
resources not copied or stale, or no record of a refresh for this checkout
(`.bdev/state/`). A refresh runs the support repository's `applyPatches.sh` and
`copyMacRes.sh`, which change Chromium files and copy resources into the checkout's
`third_party` directories (the paths its `copyMacRes.sh` declares), and then
regenerates GN files. If support patches would overwrite Chromium files that have
local edits, the build stops with `PREPARATION_CONFLICT` before changing anything.
Inputs you edited in the working copy are applied as they are.

The build also keeps a marked block of GN overrides (no component build, no
secondary ABI, `use_mold=false`, remote execution) in the output's `args.gn`.
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
`AndroidManifest.xml`, or a package that is not `com.brave.*` (checked with the
checkout's `aapt2` when present), is `ARTIFACT_MISMATCH`.

## Install and restart

```sh
bdev run android --device <id>
bdev deploy android                 # same as 'run android'
bdev build-run android --device <id>
```

Device selection: `--device`, otherwise `defaults.android_device` from your
configuration, otherwise the only usable device. With several usable devices the
command stops with `DEVICE_AMBIGUOUS` and lists ids and states; it never picks the
first or the most recent. A device that is `offline` or `unauthorized`, or not
connected, gives `DEVICE_UNAVAILABLE` with recovery steps. `build-run` chooses the
device before building.

The APK is installed over the existing app (`adb install -d -r -g`), that package
is stopped on that device only, launched, and its process confirmed. Profiles and
app data are kept: nothing is uninstalled or cleared. Older or independently built
APKs may be installed, with the same `STALE_BUILD` / `UNKNOWN_FRESHNESS` reporting
as on macOS. `adb` comes from `ADB`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, or `PATH`.

## Recovery and limits

- Failed, cancelled, or interrupted builds mark the output for revalidation; an
  older valid APK can still be installed. Nothing rebuilds or deletes for you.
- Clean outputs with `bdev clean android` ([source and cleanup](source-and-cleanup.md)).
- Release, other architectures, and AAB output are accepted but `limited`.
- Real device installation and restart, and building against real support
  revisions, have not been verified by the automated tests, which use fakes.
