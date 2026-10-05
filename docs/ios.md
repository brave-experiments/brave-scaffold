# iOS

Existing-checkout workflows for the Brave iOS app on an iOS Simulator, on an arm64
Mac. Only the Debug configuration for a simulator is supported; physical devices,
Release, and other configurations are not. Nothing here changes how Core builds iOS.

Prerequisites: a registered checkout with an approved environment
([getting started](getting-started.md)), Xcode with an iOS Simulator runtime, and
`bdev doctor ios --checkout <name>` passing. Use one operator per checkout.

## Sync and bootstrap

```sh
bdev sync ios              # adds ios to target_os and runs Core's sync
bdev sync ios,android      # both mobile targets, in a fixed order
```

`bdev sync ios` is the normal sync with `--target_os` built from the checkout's
existing `.gclient` values plus `ios` ([source and cleanup](source-and-cleanup.md#sync-sources)).
It needs Core's hooks, so `--nohooks` is refused. The `bootstrap_ios` hook runs
Core's `script/ios_bootstrap.py`, which creates placeholders under
`src/out/ios_current_link` (`args.xcconfig` and the `BraveCore`, `NalaAssets`, and
`PartitionAllocSupport` xcframeworks) and `ios/brave-ios/App/Configuration/LLDBInit`,
so Xcode can resolve the Swift package. After the sync, the scaffold checks that
these files exist and otherwise stops with `PREPARATION_CONFLICT`. The repair it
suggests is Core's own command: `bdev bpm run ios_bootstrap`.

## Readiness

`bdev doctor ios` is read-only. Besides the shared macOS host checks it reports:

| Check | Passes when |
| --- | --- |
| `ios-xcodebuild` | `xcodebuild -version` succeeds |
| `ios-simulator-sdk` | `xcrun --sdk iphonesimulator --show-sdk-version` succeeds |
| `ios-gclient-target` | `.gclient` `target_os` includes `ios` |
| `ios-project` | `Client.xcodeproj` is readable and declares an `IPHONEOS_DEPLOYMENT_TARGET` |
| `ios-bootstrap` | the files listed above exist |
| `ios-simulator` | an available simulator runtime is at least the project's deployment target |
