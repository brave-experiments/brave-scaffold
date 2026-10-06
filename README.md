# Brave development scaffold

A small command-line API for the sync, build, run, and test commands you use in
Brave Core development. `bdev` brings checkout selection, platform options,
readiness checks, and results into one tool, replacing the scripts and aliases
you would otherwise maintain for each workflow.

Brave Scaffold is optional tooling around `brave-core`. Adoption requires no
changes to Core: setup writes no integration files, hooks, Git configuration, or
exclusions inside your checkouts. Core's own commands and standalone workflow
remain available. Requested syncs, builds, and tests still make their normal
checkout writes.

## Build and run the tests you changed

After editing tests, run `bdev test mac` or `bdev test android`. It finds modified
test files in Core, chooses the suites, derives their filters, and compiles and
runs them. You do not need to look up each suite or assemble its filter yourself.

For example, suppose you changed a `*_unittest.cc` file containing the fixture
`ExampleTest` and a `*_browsertest.cc` file containing `ExampleBrowserTest`:

```sh
bdev test mac --plan   # inspect the selected suites and filters; run nothing
bdev test mac          # compile and run the selection
```

The selection is:

```text
Modified test files
├── *_unittest.cc    → brave_unit_tests     → ExampleTest.*
└── *_browsertest.cc → brave_browser_tests  → ExampleBrowserTest.*
```

One command handles both suites, with filtered runs instead of running every
test in each suite. Compilation still builds the required test targets. A test
failure in one suite does not prevent the other from running; a setup error
stops the remaining work.

Discovery includes branch commits since divergence from `origin/master`, plus
staged, unstaged, and untracked files. Use `--base REF` for another base.
Selection comes from modified **test files**, not production-code changes, and
C++ filters cover each fixture in the file, not just edited test cases.
Unsupported or unmapped files are reported. If nothing is selected, nothing runs.

The same workflow handles Android Java tests:

```sh
bdev test android --plan
bdev test android
```

JUnit files map to host-side `brave_junit_tests`; device Java tests map to
`brave_java_unit_tests`. Android tests need the
[Android test support setup](docs/android.md#tests), and device suites need a
compatible device. Desktop WebUI tests can also map to their C++ browser-test
harness. See [test selection](docs/commands.md#test) for supported file patterns,
`--file`, explicit suites, and filters.

## Check setup before starting work

`bdev doctor mac` checks the selected checkout's environment and required tools,
including checkout-local Node and the package manager, Xcode/SDK readiness, and
local Siso/RBE configuration. `bdev doctor rbe` focuses on remote-build setup:
RBE settings, native Siso mode, TLS file availability and certificate expiry,
cache configuration, and generated sync files.

Doctor reports blockers, warnings, and suggested next steps. It never installs,
repairs, or approves anything. RBE checks inspect local configuration; they do
not test VPN or service connectivity. Use `bdev doctor android` or
`bdev doctor ios` for platform checks. Outside a checkout, doctor inspects all
registered checkouts; `--checkout main` limits it to one.

## Keep the daily commands short

Once configured, these commands use the checkout containing your current
directory. Add `--checkout main` to select a registered checkout from elsewhere.

| Command | What it saves you |
| --- | --- |
| `bdev cd main` | Enter a registered checkout's `src/brave` without remembering its path. Requires the Bash/Zsh helper described below. |
| `bdev status` | See Core's branch and local changes, prior build/test outcomes for the current branch, output revalidation warnings, and a warning when free disk space falls below 200 GB. Saved outcomes are history, not proof that current code passes. |
| `bdev sync` | Run Core's sync through the selected checkout's own Node and package manager. |
| `bdev build mac` | Check readiness, prepare sources, build, and verify the output. Use `android` or `ios` for those targets. |
| `bdev run mac` | Restart the browser from an existing output without rebuilding. Android installs and restarts the APK; iOS installs and launches in a Simulator. |
| `bdev sbr mac` | Sync, build, and restart in order (`sync-build-run`). A failed phase stops the workflow; launch requires a verified output from this build. |
| `bpm run <script>` | Use a Core package script directly with that checkout's Node and package manager. |

`bdev br` means build then run; `bdev sb` means sync then build. Extra arguments
on combined commands go to the build phase. Add `--plan` to preview sync, build,
run, or combined operations without executing them.

Sync uses Core's normal resets, patches, and hooks, which can overwrite local
changes. Save wanted work before syncing, including through `bdev sbr`.

### Know when a command finishes

You can leave a build or test running and get its final outcome without watching
the terminal. By default, major operations such as sync, build, and test send a
macOS desktop notification with the checkout, elapsed time, exit code, and log
path. Combined commands send one notification for the final outcome, including
a failure to launch after a successful build.

Use `--notify=never` to silence a command or `--notify` to notify for an inspection
such as doctor. Configure the default policy in `brave-scaffold.toml`; delivery
can be a desktop notification, a terminal bell, or both. Desktop delivery depends
on macOS notification permissions; bell behavior depends on terminal settings.
See [completion notifications](docs/commands.md#completion-notifications).

## Try it with an existing checkout

The current host is **macOS arm64**. You need Python 3.14 or newer, Git, direnv,
and an existing full Brave checkout. Browser checkouts may live anywhere;
Git linked worktrees are not supported.

From this repository's root:

```sh
python3.14 -m venv --without-pip scripts/.venv
scripts/bdev setup
```

Register your checkout with
`scripts/bdev checkout add main /path/to/src/brave`, then generate its external
environment:

```sh
scripts/bdev env init --checkout main
```

Review the printed environment file and run the printed `direnv allow` command
yourself. Then check readiness:

```sh
scripts/bdev doctor mac --checkout main
```

To use the short commands in this README, add this repository's `scripts/`
directory to `PATH`. For `bdev cd`, also source `scripts/bdev-shell.sh` in Bash
or Zsh, using its absolute path in your shell startup file. Without the helper,
`bdev cd main` prints the path instead of changing directories.

You can also invoke `scripts/bdev` directly from this repository, with no `PATH`
change or shell hook. The tooling runtime needs no Python packages or activation.
Configuration and generated environments stay outside Core. See
[getting started](docs/getting-started.md) for the full setup.

## Platform scope and diagnostics

The current workflows cover macOS Debug arm64, Android arm64 APK builds and
deployment, and iOS Debug Simulator builds and launch. Android test commands
exist but are marked unverified on a real checkout; iOS tests are not available.
Native Linux and Windows hosts and fresh checkout creation are not supported.
`bdev capabilities` lists support and real-checkout validation by operation,
configuration, and architecture.

Commands show phases, effective primary commands, and live child output, and
save a redacted diagnostic log with the exact commands and working directories.
Use `--quiet` for less console output or `--verbose` to include probes.
`bdev --version` identifies the scaffold revision; logged operations also report
it so shared output can be traced to the version used.

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
instructions.

Licensed under the [Mozilla Public License 2.0](LICENSE).
