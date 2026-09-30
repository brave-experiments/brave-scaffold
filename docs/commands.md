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
- Before each subprocess the tools print its absolute working directory and full
  command to stderr, with known secrets redacted. Set `logging.commands = false`
  to turn this off; results and errors are unaffected. The same redaction covers
  command lines in results, plans, error details, and saved operation records; the
  child still receives the real arguments. Secrets that a child prints itself are
  not scrubbed. Every subprocess the tools start is logged, including probes; a check
  repeated while waiting (for example whether a process has exited) is shown once,
  followed by a line saying how many more times it ran, and the operation record keeps the
  count. Probe output is read with a size limit: output beyond it is discarded while
  reading, and evidence that needs the whole output (Git status, the process listing)
  is treated as unknown rather than as the complete answer. A logged command that timed
out, had output discarded, or left pipes abandoned by a process outside its group says so
in its command record (`timed_out`, `truncated`, `cleanup_incomplete`).
- Nothing is repaired or approved automatically. Suggested next steps in errors
  are suggestions.

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
| `bdev doctor [scope]` | Named readiness checks (`mac`, `android`, `rbe`, `shell`, `signing`) | None |
| `bdev build [target]` | Prepare, compile, and verify the output ([macOS](macos.md)) | Writes build output; may apply patches |
| `bdev test [target] <suite>` | Compile if needed and run one suite ([macOS](macos.md)) | Writes build output; runs tests |
| `bdev run [target]` | Restart the browser with an existing output; never builds | Quits and relaunches the application |
| `bdev build-run` (`br`), `sync-build` (`sb`), `sync-build-run` (`sbr`) | Combined workflows; extras go to the build phase | Effects of each phase |
| `bdev deploy android` | Install the APK on one device and launch it; same as `run android` ([Android](android.md)) | Installs over the existing app and restarts the package |
| `bdev android setup` | Create this checkout's Android support working copy | Uses the network; writes the shared object cache and the working copy |
| `bdev sync [targets]` | Core source sync ([details](source-and-cleanup.md)) | Changes sources and dependencies |
| `bdev drift [--diff]` | Compare patched Chromium files with patch metadata | None |
| `bdev patches update` | Regenerate patch files from local Chromium edits | Rewrites patch files; commits nothing |
| `bdev clean [target]` | Preview generated build outputs of the selected checkout; `--execute` deletes them ([details](source-and-cleanup.md)) | Preview writes nothing; `--execute` deletes directories under `src/out` |
| `bdev tools setup` | Explicit repair of checkout-local Node/package-manager payloads | Runs the checkout's payload installer inside the checkout |
| `bdev vpython3 [options] [--] <args>` | Checkout-local Python | Whatever the program does |
| `bpm [options] <package args>` | Checkout's package manager | Whatever the package command does |

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
plus build identity when the artifact was just built or verified, `freshness` when it was
selected for `run`), `logs`, `checks`, and `error`, and ties `status` to `exit_code`: `ok` is
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

## Plans

`--plan` (on `build`, `test`, `sync`, the combined commands, `run`, and `deploy`) shows the
whole operation and changes nothing. The plan is `data.plan.steps`; every step has the same
fields: `name`, `summary`, `status`, `reads`, `writes`, `argv`, `cwd`, `needs` (earlier
steps), `on_failure`, `cleanup`, and `detail`. `status` is `ready` or `blocked` for
prerequisites (environment approval, checkout-local tools, readiness), `current` or
`planned` for work, `resolved` or `unresolved` for choices (the artifact, the device), and
`blocked` when a step cannot run as things stand, with the reason in `detail`. A plan
reports unresolved prerequisites instead of failing; it judges them with your calling
environment (execution uses the approved one) and is not a promise that execution
succeeds. Steps include Core patch preparation, Android support preparation (with the files
it would write), the GN overrides, the build command with its output directory, output
verification, device selection, and the restart (stop running instances, install, launch).
`argv` is `null` when the final command needs tools that are not available yet. The
operation record of a real run carries the same descriptions for the steps that change
something, so a plan can be compared with what was dispatched.

## Doctor

`bdev doctor [mac|shell|signing]` runs named checks, each `pass`, `blocker`, `warning`,
`unsupported`, or `not_checked` (in text output marked ✅, ❌, ⚠️, 🚫, and ❔, with a legend line;
JSON keeps the status words), marked required or optional. Without a scope it
runs every delivered scope. A blocker in a required check gives
`READINESS_BLOCKED`; otherwise an unevaluated required check gives
`READINESS_INCOMPLETE`; both exit `3`. Optional problems are warnings and do not
fail readiness. Machine checks run even when no checkout is selected; the
checkout checks are then `not_checked`. Checking a selected checkout evaluates its
approved environment. Doctor never installs, updates, approves, or repairs.

## Support

`bdev capabilities` lists every combination of host, target, operation,
configuration, and architecture with a status. A combination is `supported` only
after real validation on a checkout; until then it is reported `unverified`.
`limited` combinations may work but are outside the validated workflow.
