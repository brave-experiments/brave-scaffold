# Configuration and environments

`brave-scaffold.toml` is the project-level configuration for Brave Scaffold. It is machine-local and ignored by Git. [`brave-scaffold.example.toml`](../brave-scaffold.example.toml) is the checked-in template; copy it, or let `bcore setup` and `bcore checkout add` create the file. Keep machine paths out of the tracked example.

## Integration stays outside Core

Scaffold configuration, environment files, and records live in the scaffold repository (`brave-scaffold.toml`, `environments/`, `.bcore/`). Setup, `env init`, `doctor`, `context`, and `env check` write nothing inside Brave Core: no `.envrc`, tracked or untracked file, Git configuration, hook, or local exclusion.

This is different from the normal checkout writes of work you request. Commands such as `bcore tools setup` (and, when they ship, builds and syncs) change the checkout as their purpose; each command's help states its side effects.

## Console output

Set the default in `brave-scaffold.toml`:

```toml
[logging]
verbosity = "normal"
```

Choose `quiet`, `normal`, or `verbose`. Command-line `--quiet`, `--verbose`, or `--verbosity normal` overrides the file for that invocation. Saved diagnostic logs do not depend on the selected console level.

## Completion notifications

Set the default in `brave-scaffold.toml`:

```toml
[notifications]
policy = "major"
delivery = "bell"
```

`policy` decides when a notification fires; `delivery` decides how it is delivered. `policy` is `always`, `major` (the default when omitted), or `never`. On the command line, `--notify` alone means `always`, and `--notify=always|major|never` overrides the file for that invocation. An invalid value is rejected before any work starts. See [notifications](commands.md#completion-notifications).

## Fields

| Field | Meaning | Default |
| --- | --- | --- |
| `schema_version` | Configuration format version. Must be `1`. | required |
| `logging.verbosity` | Console detail: `quiet`, `normal`, or `verbose`; CLI flags override it. | `"normal"` |
| `notifications.policy` | Completion notifications: `always`, `major`, or `never`; `--notify` overrides it. | `"major"` |
| `notifications.delivery` | `bell` (terminal bell), `desktop` (experimental), or `both` (includes experimental desktop delivery). | `"bell"` |
| `defaults.platform` | Target used when no target is named: `mac`, `macos`, `android`, or `ios`. | the host platform |
| `defaults.android_device` | Device id used when several Android devices are usable and `--device` is not given. | none |
| `checkouts[].core` | Path to the checkout's `src/brave` directory: absolute, starting with `~`, or relative to the configuration file. | required |
| `checkouts[].alias` | Optional name, usable with `--checkout`. It does not make a default checkout. | none |
| `checkouts[].direnv_dir` | Directory holding the checkout's `.envrc`. Relative paths resolve beside the configuration file. | set by `env init` |

Unknown fields, wrong types, and duplicate aliases, Core paths, or environment directories are rejected with the offending field and a valid example.

## Which configuration file is used

`--config <file>` wins. Otherwise the tools use the `brave-scaffold.toml` of the installation being run. Generated environments call their installation by absolute path and pass their configuration explicitly, so another `bcore` earlier on `PATH` cannot redirect them. A missing default file does not stop `--help`, `capabilities`, or path-based discovery.

## Selecting a checkout

1. `--checkout <name-or-path>` wins. A token containing `/`, starting with `.` or `~`, or absolute is a path (relative to your current directory); anything else is an alias. A path may name Core, Chromium `src`, or the outer checkout.
2. Otherwise the checkout enclosing your current directory is used, including nested directories.
3. Otherwise the command stops and lists candidates. There is no default checkout, and inherited variables such as `BRAVE_CORE_DIR` never select one.

If an outer checkout holds several source workspaces, name the Core directory. Symlinks are resolved, so an alias and a symlinked path give one identity.

An alias is optional. Running commands still needs a record that pairs the Core path with an approved environment; `bcore checkout add` and `bcore env init` update the same record.

## Two or more checkouts

Use one full checkout per branch or agent workspace, each with its own record:

```toml
schema_version = 1

[[checkouts]]
alias = "main"
core = "/work/browser/_bad_scm/main/src/brave"
direnv_dir = "environments/main"

[[checkouts]]
alias = "review"
core = "/work/browser-review/_bad_scm/main/src/brave"
direnv_dir = "environments/review"
```

Git linked worktrees are not supported as browser checkouts. Chromium's size, materialized patches, dependencies, and build outputs make a worktree an unsuitable substitute for an independent source tree. Selecting one fails with `UNSUPPORTED_CAPABILITY` before any environment loads; `bcore context` and `bcore doctor` still describe the layout. Shared Git object caches are fine; shared working files are not. Nothing is moved, converted, or deleted for you.

## Environments

Each checkout has a scaffold-owned directory containing an `.envrc`. `bcore env init --checkout <name>` creates it from a template that calls `bcore env export`, which prints the checkout's exports: the source directories, the checkout-local `depot_tools` on `PATH`, and `VPYTHON3`. Activation performs no clone, install, sync, or build. `PYTHONPATH` is added only with `--with-pythonpath`. Checkout-specific Node and package managers are never put on your global `PATH`; commands use them by absolute path in a private child process.

`env init` never overwrites a file it did not generate (it shows suggested content instead) and refuses a directory inside the checkout.

### Approval

You review and approve each environment:

```sh
direnv allow <environment-dir>
```

The scaffold never runs `direnv allow`, edits approval policy, or edits shell configuration. Any change to the file, including regeneration after moving the scaffold, invalidates the approval. Commands check the approval of exactly the configured file; a parent `.envrc` does not count.

### How commands load it

Every checkout command loads the mapped environment itself with `direnv exec`, whether or not a shell hook is active. Before loading, tool-owned variables (`BRAVE_CORE_DIR`, `BRAVE_SRC_ROOT`, `BRAVE_LAUNCHER_CHECKOUT_DIR`, and similar) are removed from the inherited environment so stale exports cannot pick a checkout. After loading, the result must agree with the selected checkout or the command stops with `CHECKOUT_ENV_CONFLICT`. This covers `run` and `deploy` too: they load and validate the environment before stopping or launching anything. The loaded environment is then the one every phase of the command uses (readiness checks, `adb` and SDK lookups, build settings such as `JAVA_OPTS`, and child processes), not the shell you started from. Loading evaluates the approved `.envrc`, which is code you reviewed; the scaffold's own template only derives exports.

### Optional shell use

`bcore shell --checkout <name>` starts a child shell in Core with the environment loaded; exiting restores your shell unchanged. An interactive direnv hook and an `.envrc` in the outer checkout directory are optional conveniences you may add yourself. If you do, do not use `source_env` to pull in the mapped environment: it skips the approval check. Never put such a file inside Core.

## Workspace activation (optional)

To type `bcore` and `bpm` without a path while working in the scaffold repository, add a workspace `.envrc` at its root that only extends `PATH`:

```sh
PATH_add scripts
```

Review it and run `direnv allow` yourself. It selects no checkout and adds no checkout tools, so it cannot change which checkout a command uses; that always comes from `--checkout` or the current directory. Leaving the directory unloads it.

## Operation records and state

Builds, tests, syncs, patch updates, run and deploy, executed cleanup (`clean --execute`; a preview writes nothing), `tools setup`, and `android setup` write a record before they change anything and complete it afterwards. Each dispatched command is added to the record (redacted) before it starts, so a killed process still shows what was running. The record holds: operation ID and CLI version, timestamps, the checkout, the environment file with its SHA-256 (never its contents), Core and Chromium revisions with Core's uncommitted-file count, target, configuration and architecture, steps, redacted commands, child exit status, the error code and message on failure, artifacts, cleanup status after a cancellation, and where logs went (`logs.commands`, `logs.child_output`; child output goes to the terminal or, with `--json`, to stderr, and no per-operation log file is written). An expected failure or a caught SIGINT/SIGTERM finishes the record with the actual status (exit 5, 130, 143, ...) and the result carries the same `operation_id`; only a process that dies leaves it incomplete.

Each phase (sync, patch application, build or test, output verification, install, stop, launch) is a step that is `running` from the moment it starts and then `succeeded`, `failed`, or, when you cancel, `interrupted`, with its actual child exit status and outcome. A step that is still `running` in an incomplete record shows where the process died. Verified artifacts are saved when they are verified. If a later phase fails, the error result keeps what earlier phases achieved: `error.details.completed_phases` lists them (for example a finished sync or a verified build) and `artifacts` holds the verified output, so a caller does not need the record to see that a build finished before its launch failed. Records, per-output build history, patch receipts, and the shared Android support cache live in `.bcore/` next to `brave-scaffold.toml` (ignored by Git), never in Core. The newest 100 completed operation records are kept; incomplete records and any record an output still refers to are never pruned, and build outputs are never touched by pruning. `bcore context` lists operations that never finished.

## Python runtime

Tooling runs on the scaffold-owned virtual environment at `scripts/.venv` (Python 3.14 or newer, created with `--without-pip`). It is separate from the checkout's `vpython3`, which runs checkout tasks.

## Checkout-local tools

`doctor` judges everything that depends on the checkout (SDK, build, remote-build, Android, and tools) in the approved environment that commands on that checkout run in, so a tool or variable that only the `.envrc` supplies counts, and one the `.envrc` removes does not. Machine, shell, and signing checks use your own environment. With no checkout selected the checkout-dependent checks use your environment and the checkout checks are `not_checked`; if the approved environment fails to load, those checks are `not_checked` too instead of passing on your environment.

Package commands use the Node, package manager, and `vpython3` inside the checkout. There is no fallback to global tools. Inspection (`doctor`, `context`, `env check`) is read-only and never installs or updates anything. If a payload is missing or stale, the command stops and names the explicit repair, `bcore tools setup --checkout <name>`, which runs the checkout's own payload installer. Run it only when you intend the checkout to change.

### Supported tool layout

The scaffold reads these checkout-local layouts and reports anything else as missing or unverifiable:

| Piece | Where it looks |
| --- | --- |
| Package manager | `devEngines.packageManager` in Core's `package.json` (`npm` or `pnpm`). No declaration means an older npm checkout; a malformed or unsupported one is `DEPENDENCY_INCOMPATIBLE`. |
| Node | `third_party/node/node-mac-<arm64\|x64>/bin/node`, or the versioned npm layout below, inside Core. |
| npm | Inside the Node payload (`lib/node_modules/npm`); npm needs no separate payload. |
| pnpm | `third_party/node/node_modules/pnpm`; only pnpm checkouts need it. |
| `vpython3` | `vendor/depot_tools` in Core, else `third_party/depot_tools` in Chromium. Core's sync installs it. |
| Verification | The checkout's own `tools/cr/extra_deps.py` says whether each required payload entry is deployed at its pinned version. Without it, or without the entry, the payload counts as unverified: a compatible version alone is not proof. |
| Repair | `bcore tools setup` runs `tools/cr/tarball_installer.py` for exactly the entries the declared manager needs (Node for npm; Node and pnpm for pnpm), then inspects the same set again. It does not install `vpython3` or Android tools. |

Declaration-absent npm checkouts also support the versioned archive layout: `third_party/node/mac_arm64/node-v<version>-darwin-arm64` (x64 uses `mac/node-v<version>-darwin-x64`). `tools/cr/install_extra_deps.py` must declare one non-overlay Node archive in a literal `EXTRA_DEPS` entry for that host, with its SHA-256. Inspection reads that literal without importing the installer, checks its `_hash.stamp` against the declared SHA, and requires the actual Node version to match the archive version. Explicit `tools setup` invokes that checkout's `install_extra_deps.py` for its single Node entry, then repeats the checks. npm ships inside the Node archive; no pnpm payload is needed. Other layouts or missing pins remain unverified.

Missing local `vpython3` requires manual restoration of depot_tools. Preserve local work, then restore a missing `vpython3` from that depot_tools repository's own HEAD. If the directory is absent or is not a Git repository, use Core's standalone setup to create a local depot_tools checkout. Scaffold sync and tools setup both need this interpreter before loading an environment, so neither is an automatic repair for its absence. The error supplies manual steps with an empty repair argv and `requires_user_action: true`.

A tool counts as local only if it resolves, after following every link, inside the checkout itself: Node and the package manager inside Core, `vpython3` inside Chromium's source root. The comparison is against the checkout's frozen canonical path, so moving a whole payload directory (`third_party/node`, `vendor/depot_tools`) outside and linking it back does not make its contents local, while links that stay inside the checkout are fine. If the checkout has no local `vpython3`, an approved environment cannot provide one: `bcore vpython3` and package commands stop with `LOCAL_TOOL_MISSING`, and `bcore tools setup` refuses to install through a `third_party/node` that leaves the checkout. It also has to be verifiable: the checkout's payload metadata must report the pinned Node (and, for pnpm checkouts, pnpm) as deployed. Without that evidence, or with a compatible version but no pin to compare it to, the command stops.

`android_support_path` is optional. It selects the shared Android-on-Mac support checkout, with paths relative to this configuration file or absolute. Its default is `brave-android-mac-support` beside the file. Only explicit `bcore android setup` on macOS creates it. See [Android support](android.md#android-on-mac-support-repository).
