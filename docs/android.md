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
   `bdev sync-build android` does this first and then checks the build's readiness
   (the Android target, and the local RBE configuration unless you compile with
   `--offline`) again; a standalone `bdev build android` needs the target already.
2. Create the checkout's own support working copy: `bdev android setup`. This is
   the only step that uses the network.

## Android-on-Mac support repository

Compiling Android from macOS needs resources and patches Chromium does not ship,
kept in a separate support repository. Each checkout gets its own working copy
at `<workspace>/brave-android-mac-support`, beside (not inside) its sources. Two
checkouts that need different revisions therefore never switch a shared tree. Only
content-addressed storage is shared: the Git object cache
(`.bdev/cache/android-support.git` next to your configuration) and the store of
Git LFS objects beside it. Working copies are local clones of the cache, so objects
are hardlinked on the same filesystem, and their large files are written from the
shared store. A local `--source` seeds both without using the network.

`bdev android setup` is also the only command that fetches large files. It writes what
the shared store holds, fetches whatever is missing from the source, and then lists the
large files that are still pointers: if any remain, it fails with `CHILD_FAILED` (a
plain `git lfs checkout` reports success while leaving pointers), and it verifies an
existing working copy the same way when you run it again after an interruption. `bdev
doctor android` (`android-support-lfs`) and builds only look: pointers are a blocker
or `DEPENDENCY_INCOMPATIBLE` that names `bdev android setup`, and nothing is fetched.

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

Before compiling, a build compares the working copy with the checkout and runs only
the support scripts that are needed: `applyPatches.sh` when support patches are not
applied (`applyPatches.sh -v` fails) or the working copy's inputs changed, and
`copyMacRes.sh` when resources are not copied or stale. Either runs when there is no
record of a refresh for this checkout (`.bdev/state/`). The scripts change Chromium
files (in the source root and in nested repositories such as `v8`) and copy resources
into the checkout's `third_party` directories (the paths `copyMacRes.sh` declares);
GN files are then regenerated.

Before running a script, the build lists what it can write: every file named by the
patches the script applies (any header prefix), the files the script edits directly,
and the destination of each copied resource. It stops with `PREPARATION_CONFLICT`,
changing nothing, when

- one of those files has unstaged, staged, or untracked changes that differ from what
  the last refresh wrote;
- a copied resource file changed since the last refresh (the first copy onto files
  that Chromium supplied is expected and is not blocked);
- a patch has a format the scaffold cannot read, or a file could belong to more than
  one repository.

Restore or move the listed files, then repeat. Edits made outside the files the
scripts touch are never affected. Inputs you edited in the working copy are applied
as they are. The scaffold finds direct edits by the `$src_root/<file>` paths in
`applyPatches.sh`; a write it does not mention there cannot be predicted.

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

The APK is installed over the existing app (`adb install -d -r -g`), the APK's own
package is stopped on that device only (a failed stop is `LAUNCH_FAILED`), launched, and
its process confirmed. Profiles and
app data are kept: nothing is uninstalled or cleared. Older or independently built
APKs may be installed, with the same `STALE_BUILD` / `UNKNOWN_FRESHNESS` reporting
as on macOS. `adb` comes from `ADB`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, or `PATH`.

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
