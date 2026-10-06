## Overview

Brave Scaffold gives Brave developers a small command-line API for common
`brave-core` operations: sync, build, run, and test. It replaces the personal
scripts and aliases you maintain to select a checkout, assemble platform-specific
commands, and run the next step after a build.

If you already have a setup you like, start with `bcore test mac`: it finds the
test files you changed, compiles their suites, and runs them with the right
filters. You can use the rest as it becomes useful.

Scaffold is optional. **Adopting it requires no changes to Brave Core.** Setup
writes no integration files, hooks, Git configuration, or exclusions inside your
checkouts. Core remains usable on its own with its normal commands. Explicitly
requested syncs, builds, and tests still make their normal checkout writes.

## Features

### Compile and run the tests you changed

After editing tests, run `bcore test mac` or `bcore test android`. Instead of
looking up test targets, building each one, and assembling filters, let `bcore`
select and run the suites for you.

Suppose you changed a `*_unittest.cc` file with the fixture `ExampleTest` and a
`*_browsertest.cc` file with `ExampleBrowserTest`:

```sh
bcore test mac --plan   # inspect the selection without building or running
bcore test mac          # compile and run both selected suites
```

```text
Changed test files          Test target             Run filter
*_unittest.cc          ->  brave_unit_tests     ->  ExampleTest.*
*_browsertest.cc        ->  brave_browser_tests  ->  ExampleBrowserTest.*
```

One command builds the required targets and runs the selected fixtures, saving
you from running every test in each suite. A test failure in one suite does not
prevent the other from running; a setup error stops the remaining work.

Discovery includes commits since your branch diverged from `origin/master`, plus
staged, unstaged, and untracked files. Use `--base REF` for another base. It selects
from changed **test files**, not production-code changes. C++ filters include all
fixtures in each selected file, not only the test cases you edited. Unmapped
tests are reported; if nothing is selected, nothing runs.

Android uses the same workflow:

```sh
bcore test android --plan
bcore test android
```

Java JUnit files map to host-side `brave_junit_tests`; device Java tests map to
`brave_java_unit_tests`. Android tests require the
[Android test support setup](docs/android.md#tests), and device suites need a
compatible device. These commands are implemented but remain unverified on a
real checkout. Desktop WebUI tests can also map to their C++ browser-test harness.
See [test selection](docs/commands.md#test) for supported file patterns and limits.

### Check your setup before a long build

`bcore doctor mac` checks the selected checkout's environment, required tools,
checkout-local Node and package manager, Xcode/SDK readiness, and local Siso/RBE
configuration. `bcore doctor android` and `bcore doctor ios` check their platforms.

Use `bcore doctor rbe` when remote-build setup is the problem. It checks RBE
settings, native Siso mode, TLS file availability and certificate expiry, the
cache directory, and generated sync files. These are local checks; they do not
verify VPN or service connectivity.

Doctor reports blockers, warnings, and suggested next steps without installing
or repairing tools. Outside a checkout it checks all registered checkouts;
`--checkout main` limits it to one.

### Switch checkouts and see where you left off

`bcore cd main` enters the registered checkout's `src/brave` directory. Give each
checkout a short alias and stop maintaining a separate `cd` alias for every path.
This requires the optional Bash/Zsh helper in the
[installation guide](docs/getting-started.md#install); without it, the command
prints the path.

`bcore status` brings together the current branch, local changes, previous
build/test outcomes for that branch, and their log paths. It also flags outputs
that need revalidation and shows free disk space when it falls below 200 GB.
Saved outcomes are history, not proof that your current source passes tests.

### Sync, build, and restart with one command

`bcore sbr mac` runs sync, build, and restart in order. A failed phase stops the
workflow, and launch requires the verified output from that build. Use
`bcore br mac` to build and restart without syncing, or `bcore sb mac` to sync and
build without launching. The same commands accept `android` and `ios` targets.
Extra arguments on combined commands go to the build phase.

Sync runs Core's normal resets, patches, and hooks, which can overwrite local
changes. Save wanted work before syncing, including through `bcore sbr`.

### Get notified when work finishes

Leave a build or test running without watching the terminal. By default, major
operations send a macOS desktop notification with the checkout, elapsed time,
exit code, and diagnostic log path. Combined commands send one final outcome,
including a failed launch after a successful build.

Use `--notify=never` to silence a command. You can choose desktop notifications,
a terminal bell, or both in configuration. Delivery depends on macOS notification
permissions and terminal settings. See
[completion notifications](docs/commands.md#completion-notifications).

## Recipes

Once configured, commands select the checkout containing your current directory.
From elsewhere, add `--checkout main`; `main` is a checkout alias, not a branch.

| What you want to do | Command |
| --- | --- |
| Enter a checkout and see its recent work | `bcore cd main`, then `bcore status` |
| Check readiness before building | `bcore doctor mac` |
| Preview which changed tests will run | `bcore test mac --plan` |
| Compile and run changed tests | `bcore test mac` |
| Run tests from one file, changed or not | `bcore test --file components/example/example_unittest.cc` |
| Run a known browser-test fixture | `bcore test mac brave_browser_tests --filter 'Example.*'` |
| Build and restart after a source edit | `bcore br mac` |
| Sync, build, and restart | `bcore sbr mac` |
| Preview sync/build/restart commands | `bcore sbr mac --plan` |
| Restart an existing macOS output without rebuilding | `bcore run mac` |
| Build and install on every connected Android device | `bcore build-run android --all-devices` |
| Build and launch in an iOS Simulator | `bcore build-run ios` |
| Call a Core package script with the checkout's own tools | `bpm run <script>` |

Commands show phases, effective primary commands, and live child output. A
redacted diagnostic log keeps the exact commands and working directories for
later inspection. Use `--verbose` to show probes or `--quiet` for less output.
See the [command reference](docs/commands.md) for options and more workflows.

## Installation

To have your agent configure it, give it this repository and the locations of
your existing checkouts:

> Look at this brave-scaffold repository. I have brave-browser checkouts at
> <insert locations>. Configure Brave Scaffold for those checkouts, including
> the shell helper for `bcore cd`, and check readiness with `bcore doctor`.
> Show me the generated environment files and the `direnv allow` commands
> I need to review and run myself.

The current host requirement is **macOS arm64**, with Python 3.14+, Git, direnv,
and an existing full Brave checkout. Scaffold and your checkouts can live in
separate directories or on different volumes; Git linked worktrees are not
supported.

For manual setup, prerequisites, and shell configuration, follow
[Getting started](docs/getting-started.md). Setup and environment approval are
one-time steps per installation/checkout, with renewed approval when an
environment file changes.

## Platform support

The main workflows cover macOS Debug arm64, Android arm64 APK builds and
deployment, and iOS Debug Simulator builds and launch. Android tests remain
unverified on a real checkout; iOS tests are unavailable. Native Linux and Windows
hosts and fresh checkout creation are not supported. `bcore capabilities` lists
support and real-checkout validation by operation, configuration, and architecture.

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
| Manage shared support repositories | [Support repositories](docs/support-repositories.md) |
| Sign commits | [Commit signing](docs/signing.md) |
| Diagnose a failure | [Troubleshooting](docs/troubleshooting.md) |
| Drive the tools from an agent | [Agent workflows](docs/agent-workflows.md) |
| Change or extend the tools | [Development](docs/development.md) |
| Project skills for contributors | [Skills](docs/skills.md) |

Read [AGENTS.md](AGENTS.md) before contributing. `CLAUDE.md` links to the same
instructions. Licensed under the [Mozilla Public License 2.0](LICENSE).
