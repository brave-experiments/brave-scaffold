## Overview

Brave Scaffold makes everyday `brave-core` development easier: building and restarting the browser, running tests, and switching between checkouts. `bcore` provides short commands for these workflows using `brave-core`'s existing tools. Use whichever commands help and keep any of your existing workflows.

Scaffold is optional. **Adopting it requires no changes to `brave-core`.** Setup writes no integration files, hooks, Git configuration, or exclusions inside your checkouts. You can keep using `brave-core` commands directly.

Currently runs on **macOS arm64**, with macOS, Android, and iOS Simulator targets. See [platform support](#platform-support) for what's been tested.

## Examples

### Build and open the browser after an edit

Consider the case where you've edited your Android code and you're ready to build and run. Assuming no local scripts/aliases, that will look like so for a Debug arm64 build (from `src/brave`, with one device connected):

```sh
pnpm run build --target_os=android --target_arch=arm64 \
  -C android_Debug_arm64 Debug --target_android_output_format=apk \
  --gn=is_component_build:false --gn=enable_android_secondary_abi:false \
  --gn=use_mold:false --gn=use_system_xcode:false --use_remoteexec=true

adb install -d -r -g ../out/android_Debug_arm64/apks/BraveMonoarm64.apk
adb shell am force-stop com.brave.browser_default
adb shell monkey -p com.brave.browser_default 1
```

Now, with `bcore`:

```sh
bcore build android
bcore run android     # installs and opens the app
```

Or combine them:

```sh
bcore build-run android
# Short form: bcore br android
```

The same workflow works for macOS: `bcore build-run mac` builds and restarts the browser. Both default to Debug arm64 builds with remote execution (RBE) enabled.

### `apply_patches`, applied for you

Before compiling, `bcore build` and `bcore test` check `brave-core`'s patches and run `apply_patches` when needed. This can change patched source files. If the checks find that applying patches could overwrite local work not explained by saved patch records, the command stops for review. See [patch preparation](docs/source-and-cleanup.md#patch-preparation) for the details.

### Compile and run the tests you changed

`bcore test mac` is a handy command that finds the test files you changed, compiles their suites, and runs them with the right filters.

Suppose you changed a `*_unittest.cc` file with the fixture `ExampleTest` and a `*_browsertest.cc` file with the fixture `ExampleBrowserTest`:

Without `bcore`, you'd identify the suites and fixture names, construct a `--filter`, then build and run each suite. 

For a macOS Debug arm64 build, from `src/brave`:

```sh
pnpm run test brave_unit_tests --filter='ExampleTest.*' \
  --target_os=mac --target_arch=arm64 -C Debug_arm64 Debug --use_remoteexec=true

pnpm run test brave_browser_tests --filter='ExampleBrowserTest.*' \
  --target_os=mac --target_arch=arm64 -C Debug_arm64 Debug --use_remoteexec=true
```

Now, with `bcore`:

```sh
bcore test mac          # compile and run both selected suites
```

```text
Changed test files          Test target             Run filter
*_unittest.cc          ->  brave_unit_tests     ->  ExampleTest.*
*_browsertest.cc        ->  brave_browser_tests  ->  ExampleBrowserTest.*
```

One command builds the required targets and runs the selected fixtures, saving you from looking up targets and assembling filters yourself.

Note: selection includes changed **test files** from branch commits and local edits. It only looks for changes to test files. If you change the code being tested without changing its tests, those tests aren't selected. See [test selection](docs/commands.md#test) for discovery rules and supported tests.

### Sync, build, and restart with one command

When you want to sync before building:

```sh
bcore sync-build-run mac
# Short form: bcore sbr mac
```

This runs sync, build, and restart in order. A failed phase stops the workflow, and the browser only restarts with the verified output from that build. The same command accepts `android` and `ios` targets.

Sync runs `brave-core`'s normal resets, patches, and hooks, which can overwrite local changes. Save wanted work before syncing. See [source operations](docs/source-and-cleanup.md) for details.

## See what will run

Use `--plan` to preview the steps before running them:

```sh
bcore build android --plan
bcore sync-build-run mac --plan
bcore test mac --plan
```

During execution, `bcore` shows phases, commands, and live child output. A redacted diagnostic log keeps the commands and working directories for later inspection. Use `--verbose` to show probes or `--quiet` for less output.

## Other useful commands

| What you want to do | Command |
| --- | --- |
| Check your setup before a long build | `bcore doctor mac` |
| Check local RBE/Siso configuration | `bcore doctor rbe` |
| Switch to a registered checkout | `bcore cd main` |
| See the branch, local changes, and previous build/test outcomes | `bcore status` |
| Run tests from one file, changed or not | `bcore test --file components/example/example_unittest.cc` |
| Run a known browser-test fixture | `bcore test mac brave_browser_tests --filter 'Example.*'` |
| Restart an existing macOS output without rebuilding | `bcore run mac` |
| Build and install on every connected Android device | `bcore build-run android --all-devices` |
| Build and launch in an iOS Simulator | `bcore build-run ios` |
| Call a `brave-core` package script with the checkout's own tools | `bpm run <script>` |

`bcore doctor`, `bcore doctor mac`, and `bcore doctor android` check for a nonempty `brave_services_key` in Core's `.env` and its `include_env=` files. Missing or unreadable files and missing or empty keys are errors. The value is never shown or validated. Ask a Brave team-mate how to obtain the key. This checks the `.env` configuration, not overrides supplied through GN arguments.

`doctor` reports problems and suggested fixes without installing or repairing anything; its RBE checks do not test VPN or service connectivity. `cd` needs the optional [shell helper](docs/getting-started.md#install) to change directories; without it, it prints the path. Saved outcomes in `status` are history, not proof that your current source passes tests.

Major operations ring the terminal bell when they finish; use `--notify=never` to silence them. See the [command reference](docs/commands.md) for more options and workflows.

**Commit signing with 1Password:** Scaffold also includes a Git signing utility. Run `bcore doctor signing` to check your setup, then follow the [commit signing guide](docs/signing.md).

## Installation

Prompt your agent with a version of this:

> Read `<repo_path>/docs/getting-started.md`. My `brave-browser` checkouts are at:
>
> - `<path/to/brave-browser>`
>
> Look at my local setup, explain what installing `brave-scaffold` would involve, and recommend a useful first command. Ask before making changes.

Prefer to set it up yourself? See [Getting started](docs/getting-started.md).

## Where Scaffold and checkouts can live

Keep your existing checkout locations. Scaffold can live in a separate folder, with checkouts beside it, elsewhere, or on another volume:

```text
work/
├── brave-scaffold/          Scaffold repository
│   ├── scripts/
│   └── brave-scaffold.toml  Registers checkout paths and aliases
├── brave-browser-main/
│   └── src/brave/           Registered as main
└── brave-browser-fix/
    └── src/brave/           Registered as fix
```

Or use Scaffold itself as your development folder and nest checkouts inside it:

```text
dev/                        Scaffold repository
├── scripts/
├── brave-scaffold.toml
├── brave-browser-main/
│   └── src/brave/           Registered as main
└── brave-browser-fix/
    └── src/brave/           Registered as fix
```

For the nested layout, keep browser checkout directories ignored by Scaffold's Git configuration. See the [FAQ](#faq) for local ignore rules. No exclusions or integration files are needed inside `brave-core`.

In either layout, put Scaffold's `scripts/` directory on `PATH` and register each checkout. With the shell helper loaded, `bcore cd main` enters that checkout's `src/brave`. Commands select the checkout from your current directory; elsewhere, pass `--checkout main`. Configuration and approved environments stay with Scaffold by default. See [Getting started](docs/getting-started.md) for setup.

## Platform support

The columns show the machine running Scaffold. The target is the platform you're building Brave for. ✅ means the command has run on a real checkout with Debug arm64 builds.

| Target | Command | macOS (arm64) | Linux | Windows |
| --- | --- | --- | --- | --- |
| macOS | `sync` | ✅ | Untested | Untested |
| &nbsp; | `build` | ✅ | Untested | Untested |
| &nbsp; | `test` | ✅ | Untested | Untested |
| &nbsp; | `run` | ✅ | Untested | Untested |
| &nbsp; | `doctor` | ✅ | Untested | Untested |
| Android | `sync` | ✅ | Untested | Untested |
| &nbsp; | `build` | ✅ | Untested | Untested |
| &nbsp; | `test` | ✅ | Untested | Untested |
| &nbsp; | `run` | ✅ | Untested | Untested |
| &nbsp; | `doctor` | ✅ | Untested | Untested |
| iOS | `sync` | ✅ | N/A | N/A |
| &nbsp; | `build` | ✅ | N/A | N/A |
| &nbsp; | `test` | Not implemented | N/A | N/A |
| &nbsp; | `run` | ✅ | N/A | N/A |
| &nbsp; | `doctor` | ✅ | Untested | Untested |

Note: iOS builds and runs have currently only been verified on the iOS Simulator.

**Linux and Windows host support PRs are welcome.** Both hosts are untested. The CLI accepts macOS, Android, and iOS targets; Linux and Windows targets are not available.

Fresh checkout creation is not supported. `bcore capabilities` lists the full status by operation, configuration, and architecture, including combinations that are implemented but unverified.

## Guides

| Task | Guide |
| --- | --- |
| Install and run a first command | [Getting started](docs/getting-started.md) |
| Build, test, and run on macOS | [macOS](docs/macos.md) |
| Build, test, and install on Android | [Android](docs/android.md) |
| Build and run on an iOS Simulator | [iOS](docs/ios.md) |
| Sync sources, patches, and cleanup | [Source and cleanup](docs/source-and-cleanup.md) |
| Configure checkouts and environments | [Configuration and environments](docs/configuration-and-environments.md) |
| Look up commands, options, results, exit codes | [Commands](docs/commands.md) |
| Diagnose a failure | [Troubleshooting](docs/troubleshooting.md) |
| Change or extend the tools | [Development](docs/development.md) |

## FAQ

### How do I clone other repositories under `~/dev`?

Clone them as usual. If `~/dev` is your Scaffold repository, add each unrelated repository's directory to **Scaffold's** `.git/info/exclude`. For example, for `~/dev/my-project`, add this line to `~/dev/.git/info/exclude`:

```text
/my-project/
```

This keeps the directory out of Scaffold's untracked files without changing the shared `.gitignore` or the nested repository. The exclusion stays local to your Scaffold clone. Directories named `brave-browser*` are already ignored by the shared `.gitignore`.

If Scaffold lives at `~/dev/brave-scaffold` instead, sibling repositories under `~/dev` are outside it and need no Scaffold ignore rule. Unrelated repositories do not need to be registered with `bcore`.

### Can I keep using my existing scripts and `brave-core` commands?

Yes. You can adopt individual commands, such as `bcore test mac`, while keeping your existing workflow. Scaffold setup leaves `brave-core` untouched. Avoid running Scaffold and another build, sync, or test process against the same checkout at the same time; Scaffold does not lock checkouts.

### How does `bcore` choose which checkout to use?

Commands select the checkout containing your current directory. From elsewhere, pass `--checkout <alias>`. You choose aliases when registering checkouts; they name directories, not Git branches. With the shell helper loaded, `bcore cd <alias>` enters that checkout's `src/brave` directory. Use Git to switch branches as usual. See [checkout configuration](docs/configuration-and-environments.md).

Read [AGENTS.md](AGENTS.md) before contributing. `CLAUDE.md` links to the same instructions. Licensed under the [Mozilla Public License 2.0](LICENSE).
