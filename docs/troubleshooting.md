# Troubleshooting

Each entry gives a symptom, what it means, and the next step. Steps marked
**you** need a person; steps marked **changes files** modify something and should
be run only when you intend that. Nothing here recommends a global fallback or
automatic approval.

## The launcher reports a missing or broken runtime

*Symptom:* `the scaffold Python runtime is missing or broken` (exit 3) or a
message that the runtime is older than Python 3.14.

*Meaning:* `scripts/.venv` does not exist, points at a removed Python, or was
built from an older one. Launchers never fall back to another Python.

*Next (changes files, scaffold-owned only):*
`python3.14 -m venv --clear --without-pip scripts/.venv`. Checkouts and their
tools are untouched.

## `CHECKOUT_REQUIRED` or `CHECKOUT_AMBIGUOUS`

*Meaning:* your current directory is not inside one checkout, or the path holds
several source workspaces. Nothing is guessed.

*Next:* run `bdev checkout list`, then repeat with
`--checkout <alias-or-path-to-src/brave>`.

## `UNSUPPORTED_CAPABILITY` about Git linked worktrees

*Meaning:* Core, Chromium, or the outer checkout is a Git linked worktree.

*Next:* select an existing full checkout. The scaffold does not convert, move, or
delete anything.

## `ENVIRONMENT_REQUIRED`

*Meaning:* the checkout has no configured environment, the file is missing, or
direnv is not installed.

*Next (changes files in the scaffold):* `bdev env init --checkout <name>`.
Install direnv if it is missing (**you**).

## `ENVIRONMENT_UNAPPROVED`

*Meaning:* direnv has not approved the exact contents of the configured
`.envrc`, either because it is new or because it changed.

*Next (**you**):* read the file named in the error, then run
`direnv allow <directory>`. Agents must not do this.

## `ENVIRONMENT_LOAD_FAILED`

*Meaning:* the approved environment ran but failed. The error includes direnv's
stderr.

*Next:* reproduce with `direnv exec <environment-dir> true`. If the file is the
generated one, check that the scaffold path in it still exists; after moving the
scaffold, run `bdev env init` again and approve the new file (**you**).

## `CHECKOUT_ENV_CONFLICT`

*Meaning:* the loaded environment names a different checkout than the one
selected, for example a hand-edited `.envrc` exporting another `BRAVE_CORE_DIR`.
The mismatched variables are listed.

*Next:* fix or regenerate the environment (`bdev env init`), then approve it
(**you**).

## `LOCAL_TOOL_MISSING`

*Meaning:* a checkout-local tool is missing, stale, or outside the version range
declared in `package.json`. The message names which. A global Node or pnpm never
satisfies this and never causes a failure by itself.

*Next (changes files in the checkout):* `bdev tools setup --checkout <name>`
runs the checkout's payload installer. Run it only when the checkout may change.
`bdev doctor` and `bdev context` show the same checks without changing anything.

If the message says freshness is unverified, the checkout has no readable payload
metadata; tools still run if their versions satisfy the declaration.

## `DEPENDENCY_INCOMPATIBLE`

*Meaning:* `package.json` declares an unsupported or malformed package manager.
Only `npm` and `pnpm` are supported.

*Next:* inspect the file named in the error. The scaffold does not edit it.

## `READINESS_BLOCKED` and `READINESS_INCOMPLETE`

*Meaning:* `bdev doctor` found a required check that fails (`BLOCKED`) or could
not be evaluated (`INCOMPLETE`). Both exit 3. The failing checks are listed with
their repairs.

*Next:* handle blockers first. `not_checked` usually means no checkout was
selected; add `--checkout`.

## `CHILD_FAILED`

*Meaning:* the package command or Python program ran and exited nonzero. The
result's `child_exit_code` is its own status; the tools exit `5`. Its output is on
your terminal (stderr in JSON mode).

## `CONFIG_INVALID`

*Meaning:* `brave-scaffold.toml` has an unknown field, wrong type, or a duplicate.
The message names the field and shows a valid example.

## Interrupted operations

Interrupting a command (Ctrl-C or SIGTERM) forwards the signal to its child
process group, waits briefly, then ends it, and reports `cancelled` with exit
`130` or `143`. Inspect the checkout before retrying anything that modifies it.
