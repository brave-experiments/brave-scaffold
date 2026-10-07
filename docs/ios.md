# iOS

Existing-checkout workflows for the Brave iOS app on an iOS Simulator, on an arm64
Mac. Only the Debug configuration for a simulator is supported; physical devices,
Release, and other configurations are not. Nothing here changes how Core builds iOS.

Source sync, Debug simulator builds, and simulator installation and launch have been
validated on a real checkout on an arm64 Mac, including opening the Simulator window.
Output deletion with `bcore clean ios --execute` has not yet been validated on a real
checkout. `bcore capabilities` reports these operations separately.

Core does not build iOS with its package build command. Its documented flow is to
bootstrap the project once, then build the `Debug` scheme of
`ios/brave-ios/App/Client.xcodeproj` in Xcode; the scheme's pre-action runs Core's own
build for BraveCore. The scaffold follows that flow with `xcodebuild` and never routes
iOS through `bpm run build`.

Prerequisites: a registered checkout with an approved environment
([getting started](getting-started.md)), Xcode with an iOS Simulator runtime, and
`bcore doctor ios --checkout <name>` passing. Use one operator per checkout.

## Sync and bootstrap

```sh
bcore sync ios              # adds ios to target_os and runs Core's sync
bcore sync ios,android      # both mobile targets, in a fixed order
```

`bcore sync ios` is the normal sync with `--target_os` built from the checkout's
existing `.gclient` values plus `ios` ([source and cleanup](source-and-cleanup.md#sync-sources)).
It needs Core's hooks, so `--nohooks` is refused. The `bootstrap_ios` hook runs
Core's `script/ios_bootstrap.py`, which creates placeholders under
`src/out/ios_current_link` (`args.xcconfig` and the `BraveCore`, `NalaAssets`, and
`PartitionAllocSupport` xcframeworks) and `ios/brave-ios/App/Configuration/LLDBInit`,
so Xcode can resolve the Swift package. After the sync, the scaffold checks that
these files exist and otherwise stops with `PREPARATION_CONFLICT`. The repair it
suggests is Core's own command: `bcore bpm run ios_bootstrap`.

## Readiness

`bcore doctor ios` is read-only. Besides the shared macOS host checks it reports:

| Check | Passes when |
| --- | --- |
| `ios-xcodebuild` | `xcodebuild -version` succeeds |
| `ios-simulator-sdk` | `xcrun --sdk iphonesimulator --show-sdk-version` succeeds |
| `ios-gclient-target` | `.gclient` `target_os` includes `ios` |
| `ios-project` | `Client.xcodeproj` is readable and declares an `IPHONEOS_DEPLOYMENT_TARGET` |
| `ios-bootstrap` | the files listed above exist |
| `ios-simulator` | an available simulator runtime is at least the project's deployment target |

## Build

```sh
bcore build ios                             # Debug build for a simulator
bcore build ios --device "iPhone 16"        # build for a named simulator or a UDID
bcore build ios --plan                      # show the steps; runs nothing
bcore build ios -jobs 4 CODE_SIGNING_ALLOWED=NO   # forwarded to xcodebuild
```

`bcore build ios` checks readiness, applies Core patches only when they are out of date
and no local edits are at risk ([source and cleanup](source-and-cleanup.md)), picks a
simulator, runs `xcodebuild`, and verifies the result. It never cleans, boots a
simulator, installs, or launches anything.

The command is:

```text
xcodebuild -project <core>/ios/brave-ios/App/Client.xcodeproj -scheme Debug -configuration Debug \
  -sdk iphonesimulator -destination 'platform=iOS Simulator,id=<udid>' \
  -derivedDataPath <src>/out/ios_Debug_xcode_derived_data <your arguments> build
```

It runs in the Core directory with the checkout's approved environment and local tools.
Core's pre-action builds the GN output `src/out/ios_Debug_arm64_simulator` and repoints
`src/out/ios_current_link` at it; Xcode writes the app under the derived-data directory
(`Build/Products/Debug-iphonesimulator/Client.app`).

**Simulator choice.** `--device` takes a simulator name or UDID (exact match). Without it
the scaffold uses a booted iPhone, else the iPhone on the newest runtime (by name). Only
simulators whose runtime is at least the project's `IPHONEOS_DEPLOYMENT_TARGET` are
considered. Physical devices are refused.

**Forwarded arguments.** Unknown options and extra arguments go to `xcodebuild` unchanged,
after the generated ones and before the action. Forwarded values decide what is built, so
they are also read:

- `-derivedDataPath <dir>` replaces the default; a relative path is relative to the Core
  directory, and the app is looked for under it.
- `-destination` replaces the simulator destination. It cannot be combined with `--device`
  (`SELECTOR_CONFLICT`), and `build-run` refuses it because the simulator to run on would
  be unidentified.
- Build-setting assignments and `-xcconfig` can change the app's name, platform, or output
  directory. They are forwarded, but leave the artifact unresolved, so `build-run` stops
  without installing an older app. `CODE_SIGNING_ALLOWED`, `CODE_SIGNING_REQUIRED`, and
  `CODE_SIGN_IDENTITY` do not change output selection and can be forwarded with verification.
- `-project`, `-workspace`, `-scheme`, `-target`, `-alltargets`, `-configuration`, `-sdk`, and
  `-arch` would change what is built, so they stop the command before any change
  (`SELECTOR_CONFLICT`). `--configuration release` is `UNSUPPORTED_CAPABILITY`.
- Information and non-building modes (`-showBuildSettings`, `-list`, `-showsdks`, and the like)
  and actions other than `build`/`clean` leave the artifact unresolved: the build succeeds with an
  `ARTIFACT_UNRESOLVED` warning and no artifact. In `build-run` that is an error and nothing is
  installed or launched. Modes that may write (`-resolvePackageDependencies`, `-exportArchive`, ...)
  mark the outputs as needing revalidation; information modes leave records unchanged.

`--offline`, `--force-gn`, and `--skip-support-refresh` do not apply to iOS. Xcode's pre-action
starts Core's build with only `PATH`, so the scaffold cannot pass build options or environment
settings to it; whether that build uses remote execution comes from Core's own configuration.

### Build results

| Outcome | Result |
| --- | --- |
| `xcodebuild` exits nonzero | `CHILD_FAILED`, exit 5; both outputs are marked as needing revalidation |
| Exit 0, `Client.app` is a Brave simulator app and `ios_current_link` points at the GN output | `ok`, the app in `artifacts` |
| Exit 0 but the app is missing, not a Brave simulator build, or the link points elsewhere | `ARTIFACT_MISSING` or `ARTIFACT_MISMATCH`, exit 5 |
| Exit 0 from a mode that builds no app, or settings that leave its output unidentified | `ok` with `ARTIFACT_UNRESOLVED` (`build`), or that error with nothing launched (`build-run`) |

A failed or interrupted build can partly overwrite earlier output. The scaffold marks the GN
output and the derived-data directory as needing revalidation before `xcodebuild` starts, keeps
their earlier records as history, and does not clean or roll back. An earlier app may still be
run with `bcore run ios`.
`run` warns before installing when an attempt may have partly overwritten the selected
output, tracked sources changed since its build, or its build freshness cannot be checked.
Changed files are compared by their contents, not their size or timestamps, so an edit that
restores a file's timestamp is still noticed. A build recorded before that comparison existed
reports its freshness as unknown until it is rebuilt.

## Run

```sh
bcore run ios                               # install and launch the existing build
bcore run ios --device "iPhone 16 Pro"
bcore build-run ios                         # build, then run exactly that app
bcore sync-build ios                        # sync, then build
```

`run` never builds. It boots the chosen simulator and waits for it (`simctl boot`,
`simctl bootstatus -b`), opens that device's Simulator window, installs the app over the existing one so app data is kept
(`simctl install`), and launches it, replacing a running copy
(`simctl launch --terminate-running-process`). The launch must report a process id. Each
step that fails is `LAUNCH_FAILED` and names its phase. Failure to open the window
stops before installation and reports `open-simulator`. After launch, the command
checks that the process stays running for one second; an early exit reports
`verify-app-running`. This catches immediate exits, but does not prove the app's UI
loaded or that it will stay running afterward.

`--artifact <path to Client.app>` runs a specific build. If more than one recorded build matches,
`run` stops with `ARTIFACT_AMBIGUOUS`. `bcore clean ios` removes the GN output and derived-data
directory; after a clean, `out/ios_current_link` dangles until Core's next build repoints it, and
`bcore doctor ios` then reports missing bootstrap files (repair: `bcore bpm run ios_bootstrap`).

Not available for iOS: `bcore test` and physical devices.
