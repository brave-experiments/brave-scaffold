# Commands

`bdev <command> --help` is the authoritative reference for arguments, defaults,
and side effects. This page is the readable guide to what exists, how results
look, and what the codes mean. Only delivered commands appear here.

## Common behavior

- Scaffold options `--checkout`, `--config`, and `--json` (alias `--format json`)
  may appear before or after the command, up to `--`. `--flag=value` and
  `--flag value` both work. Repeating an option with the same value is fine; with
  different values it fails. Option abbreviations are not accepted.
- The checkout comes from `--checkout`, otherwise from the current directory.
  See [configuration](configuration-and-environments.md#selecting-a-checkout).
- Text output is the default. With `--json`, stdout holds exactly one result
  document, including for parse errors and child failures. Child output and
  command logs go to stderr.
- Normal output shows phases, primary commands with their working directories, live
  child output, and the final result. Package commands use the short `pnpm` or `npm`
  name on the console; the log keeps their exact Node and payload paths. Internal
  probes go to the saved log.
  Routine Android Java bytecode rewrite messages, in app builds and Android tests, appear
  only with `--verbose`. Empty bytecode action headings and stream labels are also
  hidden; actions with other output keep their context. The diagnostic log keeps
  all output at every verbosity level.
  `--verbose` also prints probes; `--quiet` hides progress and child output, but keeps
  scaffold warnings, errors, and results. A failed child shows its last 40 lines
  (at most 16 KiB) in quiet mode. Quiet does not try to classify child warning text.
  `--verbosity normal` restores normal output when configuration selects another level.
  These options must precede the first program argument for `bpm` and `vpython3`;
  use `--` to forward a conflicting option to a package command.
- Set `[logging] verbosity = "normal"` (or `"quiet"` / `"verbose"`) in configuration
  for a persistent default. CLI verbosity wins.
- Each configured invocation saves a mode-0600 log under `.bdev/logs/` beside its
  configuration, at every verbosity level. `--plan` previews create no log files.
  Pure `env export` and checkout navigation with `cd` create no log and remain silent on stderr on success. The final stderr line reports elapsed
  time and the log path, including in JSON mode. The JSON envelope stays unchanged.
  Logs contain effective commands, directories, phase messages, probe exit status,
  and streamed child output. Captured probe payloads are excluded because they may
  contain the environment or private data. Logs remain until manually removed;
  remove old files from this directory when no running command needs them.
- Known secret arguments, secret-named environment values, and URL passwords are
  redacted from saved child output and its console copy. This cannot identify every
  secret a program might print. When stdout and stderr are terminals, text-mode
  execution uses a pseudo-terminal with the caller's dimensions and forwards resize
  events. Children keep terminal detection, color, and interactive progress while
  their combined stdout/stderr is captured. Quiet mode changes display, not terminal
  detection. Redirected and JSON output use pipes and remain non-interactive.
  Output is forwarded by line, including carriage-return progress; a partial line
  waits for its terminator or process exit. Lines exceeding 1 MiB are explicitly
  omitted to bound memory and avoid storing partial secrets.
  Direct `bpm` and `vpython3` stdout (or combined terminal output) stays raw and
  unredacted in normal text mode so prompts and binary output still work; its saved
  copy is redacted text.
  `bdev shell` keeps direct terminal access, including prompts and output, at every
  verbosity level; its interactive output is not captured. The child receives the
  original arguments and environment.
- Completion notifications are described under
  [Completion notifications](#completion-notifications); `--notify[=POLICY]` is accepted by
  every command, including `bpm` and `vpython3` before their first program argument.
- Builds show the target, configuration, architecture, output directory, and local
  or RBE mode before compilation. Phase timings separate environment loading,
  readiness, source preparation, the package build, output verification, and source
  state inspection. The package time includes Core's preparation and compilation;
  it is not just Siso's reported time. Other time covers work outside those phases.
  Timings are saved in quiet mode and printed in normal and verbose modes.
- Probe output is read with a size limit. Incomplete evidence is treated as unknown.
  Command records flag timeouts, truncated output, and incomplete cleanup. Repeated
  poll commands keep a count instead of flooding the terminal. Source-state scans
  update one terminal line, or print progress at most every ten seconds when redirected.
- Requested builds prepare sources automatically within their declared scope. Android
  support refresh may replace local files; `--skip-support-refresh` stops a build when
  that refresh is needed. See [Android preparation](android.md#what-preparation-changes).
  Environments are never approved automatically. Suggested next steps in errors are suggestions.

### Readable reports

Doctor uses readable labels; JSON keeps stable check names. A note such as
"affects android build" says which operation needs attention, even when the check
is not required for the selected doctor scope. A refresh warning means refresh is
needed and can be attempted after checks pass, not that it is guaranteed to run.

Terminal errors show paths, reasons, and suggested commands, with a short limit on
long evidence lists. Use `--json` for the full returned evidence. Diagnostic logs
retain the structured error details. Repairs are suggestions and may need further
checks or your action.

## Support repository command

`scripts/sync-support-repos` manages shared repositories separately from `bdev`.
It accepts status, prune, and explicit discard operations
without selecting a browser checkout. See [support repositories](support-repositories.md).

## Command index

| Command | Purpose | Side effects |
| --- | --- | --- |
| `bdev setup` | Check prerequisites, create `brave-scaffold.toml` if missing, list next steps | Writes the scaffold configuration only |
| `bdev checkout add <name> <path>` | Register an existing checkout under an alias | Edits `brave-scaffold.toml` |
| `bdev checkout list` | Show registrations, environment state, invalid entries | None |
| `bdev env init` | Generate the scaffold-owned `.envrc`, print the approval command | Writes the environment file and the record; never approves |
| `bdev env export --format bash` | Print exports for a generated `.envrc` | None; runs no direnv |
| `bdev env check` | Compare the loaded environment and tools to the checkout | None; evaluates the approved `.envrc` |
| `bdev shell` | Child shell in Core with the environment loaded | Whatever you do in the shell |
| `bdev context` | Resolved checkout, selection source, environment, tools | None |
| `bdev capabilities` | Supported, limited, unverified, unsupported combinations | None; needs no checkout |
| `bdev doctor [scope]` | Named readiness checks (`mac`, `android`, `ios`, `rbe`, `shell`, `signing`) | None |
| `bdev build [target]` | Prepare, compile, and verify the output ([macOS](macos.md), [iOS](ios.md)); iOS runs `xcodebuild` | Writes build output; may apply patches |
| `bdev test [target]` | Run the tests changed on this branch or in the working tree ([details](#test)) | Reads Git state; effects of each suite it runs |
| `bdev test [target] --file PATH` | Run the tests in one file, changed or not ([details](#test)) | Same |
| `bdev test [target] <suite>` | Compile if needed and run one suite ([macOS](macos.md), [Android](android.md#tests)); `--device` or `--all-devices` for Android device suites | Writes build output; runs tests; Android also applies the support test overlay to Core's `build/commands` |
| `bdev run [target]` | Restart the browser with an existing output; never builds | Quits and relaunches the application |
| `bdev build-run` (`br`), `sync-build` (`sb`), `sync-build-run` (`sbr`) | Combined workflows; extras go to the build phase | Effects of each phase |
| `bdev deploy android` | Install the APK and launch it; same as `run android`; `--all-devices` selects all compatible devices ([Android](android.md)) | Installs over the existing app and restarts the package on selected devices |
| `bdev android setup` | Prepare shared Android-on-Mac support and link this workspace | Uses the network; writes the shared checkout and preserves existing workspace copies |
| `bdev sync [targets]` | Core source sync ([details](source-and-cleanup.md)) | Changes sources and dependencies |
| `bdev drift [--diff]` | Compare patched Chromium files with patch metadata | None |
| `bdev patches update` | Regenerate patch files from local Chromium edits | Rewrites patch files; commits nothing |
| `bdev clean [target]` | Preview generated build outputs of the selected checkout; `--execute` deletes them ([details](source-and-cleanup.md)) | Preview writes nothing; `--execute` deletes directories under `src/out` |
| `bdev tools setup` | Explicit repair of checkout-local Node/package-manager payloads | Runs the checkout's payload installer inside the checkout |
| `bdev vpython3 [options] [--] <args>` | Checkout-local Python | Whatever the program does |
| `bpm [options] <package args>` | Checkout's package manager | Whatever the package command does |

Sync commands run Core's package `sync` script with its normal resets, patches,
and hooks. Local changes may be overwritten. Scaffold forwards Core options
without adding an overwrite prompt or backup; see [sync behavior](source-and-cleanup.md#sync-sources).

### Forwarding to package commands

`build`, `test`, `sync`, `sync-build`, `sync-build-run`, `build-run`, and
`patches update` run a package script. They use only their documented
positionals and scaffold options; every other argument goes unchanged to that
script, after the generated ones, in the same order. Unknown options and extra
positionals are not errors. A value after an unknown option is never read as the
target. Use `--` to forward a token that is also a scaffold option
(`bdev build -- --json`). The first `--` is consumed; later ones are forwarded.
Combined commands send the tail to their build phase only; `sync` receives none
of it. Commands that run no package script (`context`, `doctor`, `run`, `clean`,
`drift`) reject extra arguments. The effective command and directory are logged
and recorded so the destination is clear.

## Direct tools

### `bpm`

```sh
bpm [--checkout <name-or-path>] [--config <file>] [--json] <package arguments...>
```

Scaffold options are accepted only before the first package argument; from that
argument on, every token (including `--json` and `--checkout`) goes to the
package manager. A leading `--` ends scaffold options: `bpm -- --help` asks the
package manager for help, `bpm --help` (or `bpm --checkout main --help`, help among the
leading scaffold options) shows scaffold help, and `bpm run test --help` passes `--help`
to the package manager. `bdev vpython3` follows the same rule: `bdev vpython3 script.py
--help` runs the script with `--help`.

The package manager comes from `devEngines.packageManager` in Core's
`package.json` (`npm` or `pnpm`). A checkout with no declaration is an older npm
checkout. For older npm, `bpm run sync --force` runs `npm run sync -- --force`;
an existing `--` and every argument boundary are preserved. Malformed or
unsupported declarations are errors.

The command runs in the Core directory using the checkout's Node and package
manager (by absolute path, first on the child's `PATH`) and stops before starting
if either is missing, stale, or outside the declared version range.
`bpm` exits `5` with the child's status in `child_exit_code` when the package
command fails.

Commands that do not forward arguments (for example `capabilities`, `context`, `doctor`,
`clean`) reject anything after a `--` with `INVALID_INPUT` and run nothing; an empty
trailing `--` is accepted.

### `bdev vpython3`

```sh
bdev vpython3 [--checkout <name-or-path>] [--cwd <directory>] [--] <arguments...>
```

Runs the checkout's vendored `vpython3`. The checkout only chooses the
interpreter and environment; relative script, input, and output paths use your
current directory, even when `--checkout` names another checkout.
`--cwd <directory>` (relative to your current directory) changes only where the
program runs. The result reports the interpreter path and the actual working
directory. System Python is never substituted.

## Results

Every command's JSON document has the same top-level fields, defined by
[`scripts/schemas/result-envelope.schema.json`](../scripts/schemas/result-envelope.schema.json):

```json
{
  "schema_version": 1,
  "status": "error",
  "command": "env check",
  "operation_id": null,
  "context": {"checkout": null, "selection_source": null},
  "data": null,
  "checks": [],
  "warnings": [],
  "error": {
    "code": "CHECKOUT_REQUIRED",
    "message": "No checkout is selected: the current directory is not inside a Brave checkout.",
    "details": {"candidates": ["main"], "example": "--checkout /path/to/src/brave"},
    "repairs": [{"argv": ["bdev", "checkout", "list"], "cwd": null, "requires_user_action": false}]
  },
  "artifacts": [],
  "logs": [],
  "exit_code": 2,
  "child_exit_code": null
}
```

`status` is `ok`, `error`, `partial`, or `cancelled`. Additive fields are
compatible; removing a field or changing its meaning changes `schema_version`.

The `data` of each command has its own published shape in
[`scripts/schemas/command-data.schema.json`](../scripts/schemas/command-data.schema.json);
the envelope schema selects it by `command` (`build`, `build-run`, `sync-build`, and
`sync-build-run` share one shape, `run` and `deploy` another). `data` is `null` in error
results, or a plan (`data.plan.steps`, see [Plans](#plans)) under `--plan`. The envelope
schema also types `artifacts` (an application or an APK: `path`, `kind`, `name`, `verified`,
plus build identity when the artifact was just built or verified), `logs`, `checks`, and `error`, and ties `status` to `exit_code`: `ok` is
exit `0` with `error` null, `error` is exit `1` to `5`, `partial` is `6`, and `cancelled`
is `130` or `143` with no artifacts. An unresolved build output is `ok` with an
`ARTIFACT_UNRESOLVED` warning and `artifacts: []` for `build` and `sync-build`, and an
`ARTIFACT_UNRESOLVED` error for the combined run commands.

`requires_user_action` says whether a repair step needs a person, such as
approving an environment. `false` does not mean you may run it: agents still need
authority for the action ([agent workflows](agent-workflows.md)).

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Completed. Warnings may remain. |
| 1 | Unexpected internal failure (`INTERNAL_ERROR`). |
| 2 | Invalid or missing input, ambiguity, or an unsupported request. |
| 3 | Setup, environment approval, tools, or readiness missing. |
| 4 | Local-work, preparation, or ownership conflict. |
| 5 | A child or tool failed, or required output validation failed. `child_exit_code` holds the child's own status. |
| 6 | Batch finished with partial failure or skipped required work. |
| 130 / 143 | Interrupted by SIGINT / SIGTERM after best-effort cleanup. |

### Error codes

`INVALID_INPUT`, `SELECTOR_CONFLICT`, `CONFIG_INVALID`, `CHECKOUT_REQUIRED`,
`CHECKOUT_AMBIGUOUS`, `CHECKOUT_NOT_FOUND`, `UNSUPPORTED_CAPABILITY` (for example
a Git linked worktree or an unavailable platform), `ENVIRONMENT_REQUIRED`,
`ENVIRONMENT_UNAPPROVED`, `ENVIRONMENT_LOAD_FAILED`, `CHECKOUT_ENV_CONFLICT`,
`LOCAL_TOOL_MISSING`, `DEPENDENCY_INCOMPATIBLE`, `DEVICE_AMBIGUOUS`, `DEVICE_UNAVAILABLE`, `READINESS_BLOCKED`,
`READINESS_INCOMPLETE`, `PREPARATION_CONFLICT`, `OWNERSHIP_CONFLICT`, `ARTIFACT_MISSING`,
`ARTIFACT_MISMATCH`, `ARTIFACT_UNRESOLVED`, `ARTIFACT_AMBIGUOUS`, `LAUNCH_FAILED`,
`CHILD_FAILED`, `CANCELLED`, and `INTERNAL_ERROR`.

When a later phase of a combined command fails, `error.details.completed_phases` lists the
phases that finished (each with its `phase` name and outcome, for example a sync with its
revisions or a build with its exit status and `artifact_status`), and `artifacts` keeps the
verified output. A cancelled command lists them the same way.

Sync results include the dispatched `argv` and `revisions_before` and
`revisions_after` for Core and Chromium. Core determines which updates to perform;
Scaffold does not report a predicted reset or hook write scope.

## Plans

`--plan` (on `build`, `test`, `sync`, the combined commands, `run`, and `deploy`) shows the
whole operation and changes nothing. The plan is `data.plan.steps`; every step has the same
fields: `name`, `summary`, `status`, `reads`, `writes`, `argv`, `cwd`, `needs` (earlier
steps), `on_failure`, `cleanup`, `detail`, and `conditional_arguments`. `status` is `ready` or `blocked` for
prerequisites (environment approval, checkout-local tools, readiness), `current` or
`planned` for work, `resolved` or `unresolved` for choices (the artifact, the device), and
`blocked` when a step cannot run as things stand, with the reason in `detail`. A plan
reports unresolved prerequisites instead of failing; it judges them with your calling
environment (execution uses the approved one) and is not a promise that execution
succeeds. Steps include Core patch preparation, Android support preparation (with the files
it would write), the GN overrides, the build command with its output directory, output
verification, device selection, and the restart (stop running instances, install, launch).
`argv` is `null` when the final command needs tools that are not available yet.
`writes` lists the files or directories affected by a step. Patch preparation lists
the patch targets, metadata, and version files. Sync lists the workspace `.gclient`
and source tree as general write locations; Core determines the actual files and
other effects at runtime. The readable text names a few entries and a count.
Decisions that depend on an earlier phase are not guessed: after a sync the patch
step is `unresolved`, and `conditional_arguments` lists what the build command adds only if
preparation or a sync changes files (`--force_gn_gen`), while an explicit `--force-gn`
is already in `argv`. The operation record of a real run carries the same descriptions for the steps that change
something, so a plan can be compared with what was dispatched.

## Doctor

`bdev doctor [mac|android|ios|rbe|shell|signing]` runs named checks, each `pass`, `blocker`, `warning`,
`unsupported`, or `not_checked` (in text output marked ✅, ❌, ⚠️, 🚫, and ❔, with a legend line;
JSON keeps the status words), marked required or optional. Without a scope it
runs every delivered scope. A blocker in a required check gives
`READINESS_BLOCKED`; otherwise an unevaluated required check gives
`READINESS_INCOMPLETE`; both exit `3`. Optional problems are warnings and do not
fail readiness. Outside a checkout, doctor inspects every configured checkout and runs shared
runtime, shell, and signing checks once. Each checkout uses its own approved
environment and gets a separate report. Matching host and tool results appear
once in the shared text section; differing results stay under each checkout.
Disk space appears once per filesystem, listing the affected checkouts. JSON
retains all per-checkout checks, including the shared results; any required failure makes the overall
command fail. JSON includes per-checkout reports in `data.checkouts`, with
checkout-qualified names in the top-level checks and error summary.
An explicit `--checkout` limits inspection to that checkout; running inside a
checkout selects it instead. With no configured or selected checkout, machine
checks still run and checkout checks are `not_checked`. Checking a selected checkout evaluates its
approved environment. Text reports group checks by area, show shared checks once,
and end with a readiness summary and distinct repair suggestions. With no
configured or selected checkout, dependent checks appear as one selection notice in text;
JSON retains each unevaluated check. Select one explicitly with
`bdev --checkout <alias> doctor`. Signing checks inspect configuration; they do
not prove that a signing attempt will succeed.
Doctor never installs, updates, approves, or repairs.

## Support

`bdev capabilities` lists every combination of host, target, operation,
configuration, and architecture with a status. A combination is `supported` only
after real validation on a checkout; until then it is reported `unverified`.
`limited` combinations may work but are outside the validated workflow.

## test

`bdev test` selects tests three ways. The selectors are mutually exclusive, and a conflict
is refused before any work starts.

| Form | Selection |
| --- | --- |
| `bdev test [mac\|android] [--base REF]` | Test files changed on this branch, for the named platform, otherwise the configured platform, otherwise the host |
| `bdev test [mac\|android] --file PATH` | The tests in that file, whether or not it changed |
| `bdev test [mac\|android] <suite> [--filter PATTERN]` | One whole suite, or the filtered part of it |

Add `--plan` to show the selection without running anything. Discovery reads Git state only.

Default discovery compares the selected checkout's Core with `--base` (default
`origin/master`). It includes commits on the branch since it diverged from the base, plus staged,
unstaged, and untracked files. Pushing the branch changes nothing: the remote-tracking
branch and push status are not consulted. Deleted tests are ignored. "Changed tests"
means tests in modified test files; tests are not inferred from production-code changes.
Changed test files for another platform are listed as not selected.

`--file` takes one path, absolute or relative to the directory where you run `bdev`. It must be a file
in the selected checkout's Core. Without a platform argument, the file decides the platform.

Filters are built from the files:

| File | Suite | Filter |
| --- | --- | --- |
| `*/junit/*.java` | Android `brave_junit_tests` | `package.Class.*` (`*Class.*` without a package) |
| `*/javatests/*.java` | Android `brave_java_unit_tests` | `Class.*` |
| `*_unittest.cc` | macOS `brave_unit_tests` | `Fixture.*` for each fixture in the file |
| `*_browsertest.cc`, `*_uitest.cc` | macOS `brave_browser_tests` | `Fixture.*` for each fixture in the file |
| desktop WebUI `.ts`/`.js` under `chrome/test/data/webui` | macOS `brave_browser_tests` | the C++ harness that registers the changed Mocha suite (all suites in the file with `--file`) |

The output lists the suites and filters that will run, and every file it cannot map (for
example, C++ tests under an `android` or `ios` directory, or an unsupported file type) with the reason.
If nothing is selected, the command says `No tests were selected.` and runs nothing; it never
falls back to a whole suite. `--filter` needs a named suite.

Phases run quick host suites first (JUnit, device Java, unit, browser) and all run even
if one has a test failure; setup errors stop the remaining phases. The command
fails and lists each phase's outcome. Android requirements are checked, and the device
chosen, before the first build. Extra forwarded arguments go to every suite's build and run.
iOS has no tests and fails with `UNSUPPORTED_CAPABILITY`.

`--filter` reaches the suite's runner as a gtest-style filter. For
`brave_junit_tests` use a fully qualified class (`org.example.SomeTest.*`) or a
wildcard (`*SomeTest*`); a bare `SomeTest.*` can match no tests.

Device-backed tests also accept `--all-devices`, and the terminal picker offers
`a` for All. The suite builds once, then runs on each usable device with a
compatible ABI. A failed run does not stop the remaining devices. Each device
gets its own results file and entry in `data.devices`; any failed run makes the
command return a nonzero exit. Discovery with `--all-devices` uses the same selection
for device suites and still runs host suites on this Mac. Host-only suites
reject `--all-devices`. It cannot be combined with `--device`.
If you forward `--json-results-file`, its filename receives a distinct suffix
for each device so results are not overwritten. Plans never prompt or run tests.

## cd

`bdev cd main` and `bdev cd alt-1` enter the selected checkout’s `src/brave`
directory when `scripts/bdev-shell.sh` is sourced in Bash or Zsh and `scripts/`
is on `PATH`. The function also accepts `--notify` and `--notify=POLICY` with the
checkout name. Other commands pass through to the launcher. Without the shell
function, `bdev cd <checkout>` prints the resolved directory. An unknown checkout
returns an error and leaves the current directory unchanged.

## Completion notifications

`bdev` can post one macOS desktop notification when a command finishes. It uses the
system `osascript` command: no service runs, nothing is installed, and Brave Core is
not involved. Choose the policy with `[notifications] policy` in `brave-scaffold.toml`
(default `major`) or per invocation with `--notify` (meaning `always`) or
`--notify=always|major|never`. The command-line value wins over the file.

| Policy | Notifies for |
| --- | --- |
| `major` | `sync`, `build`, `build-run`, `sync-build`, `sync-build-run`, `test`, `run`, `deploy`, `setup`, `env init`, `tools setup`, `android setup`, `patches update`, and `clean --execute` |
| `always` | Everything in `major`, plus any other command that actually ran, such as `cd`, `context`, `doctor`, `drift`, `checkout`, `env check`, `capabilities`, `shell`, `vpython3`, and `bpm` |
| `never` | Nothing |

Commands are classified by operation, not duration, so a quick build still notifies
under `major`. Help, pure `env export`, `--plan` previews, a `clean` without
`--execute`, and rejected command lines never notify under any policy.

A combined command sends one notification with the final outcome: a successful build
followed by a failed launch reports the failure. Success, failure, and handled
cancellation (Ctrl-C) are each reported. The text holds the command, checkout name,
elapsed time, exit code, error code, and the diagnostic log path. It never includes child
output, error messages, environment values, or browser arguments.

### Delivery

`[notifications] delivery` chooses how a notification that fires is delivered; the
policy and `--notify` only decide when. There is no command-line option for delivery.

| Delivery | Behavior |
| --- | --- |
| `desktop` | The macOS notification described above (default). |
| `bell` | One terminal bell per notified invocation. |
| `both` | Each method is attempted once; one failing or being unavailable does not stop the other. |

The terminal bell is the BEL character (`\a`) written to the controlling terminal
(`/dev/tty`), never to stdout, stderr, or the saved log, so redirected and JSON output
are unchanged. Your terminal's settings decide what it does: it may make a sound, flash
the window or tab, or do nothing. With no controlling terminal (for example, in a
background job without one), the bell is skipped; no other method is substituted. A
bell that fails to write prints one line on stderr and never changes the exit status.

Notification Center shows the log path as text; it is not clickable. If delivery
fails (for example, notifications are blocked for the terminal application), the
exit status and stdout are unchanged and stderr gets one line saying so. macOS
attributes these notifications to the application that runs `osascript`, so allow
notifications for your terminal in System Settings. Other hosts send no desktop notification.
