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
  to turn this off; results and errors are unaffected.
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
| `bdev doctor [scope]` | Named readiness checks (`mac`, `shell`) | None |
| `bdev tools setup` | Explicit repair of checkout-local Node/package-manager payloads | Runs the checkout's payload installer inside the checkout |
| `bdev vpython3 [options] [--] <args>` | Checkout-local Python | Whatever the program does |
| `bpm [options] <package args>` | Checkout's package manager | Whatever the package command does |

Building, testing, running, syncing, cleaning, drift inspection, and patch
updates are not available yet.

## Direct tools

### `bpm`

```sh
bpm [--checkout <name-or-path>] [--config <file>] [--json] <package arguments...>
```

Scaffold options are accepted only before the first package argument; from that
argument on, every token (including `--json` and `--checkout`) goes to the
package manager. A leading `--` ends scaffold options: `bpm -- --help` asks the
package manager for help, `bpm --help` shows scaffold help.

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
`LOCAL_TOOL_MISSING`, `DEPENDENCY_INCOMPATIBLE`, `READINESS_BLOCKED`,
`READINESS_INCOMPLETE`, `CHILD_FAILED`, `CANCELLED`, and `INTERNAL_ERROR`.
Codes for artifacts, devices, and preparation appear when those commands ship.

## Doctor

`bdev doctor [mac|shell]` runs named checks, each `pass`, `blocker`, `warning`,
`unsupported`, or `not_checked`, marked required or optional. Without a scope it
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
