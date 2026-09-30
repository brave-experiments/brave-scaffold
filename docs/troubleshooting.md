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

## `PREPARATION_CONFLICT`

*Meaning:* preparing sources could overwrite local work: patch application would
touch a Chromium file that differs from both the patch metadata and the last
recorded state (or has no record), or a sync found uncommitted Core changes. The
files are listed. Nothing was changed.

*Next:* review with `bdev drift --diff`. Keep wanted edits with
`bdev patches update`, or restore the files yourself; then repeat the command.
The scaffold never stashes, resets, or switches branches.

## `SELECTOR_CONFLICT`

*Meaning:* an explicit scaffold choice disagrees with a forwarded argument (for
example `--configuration release` and a forwarded `Debug`), or an option was given
twice with different values. Both values and an example are shown; nothing ran.

## `ARTIFACT_MISSING`, `ARTIFACT_MISMATCH`, `ARTIFACT_AMBIGUOUS`

*Meaning:* `run` found no application for the checkout, target, configuration and
architecture (`MISSING`), found one that is not a usable Brave application
(`MISMATCH`), or found several (`AMBIGUOUS`). `run` never builds.

*Next:* `bdev build` or `bdev build-run`; or choose with `--artifact <path>`. After
a build, `MISSING` or `MISMATCH` means the build succeeded but its expected output
is absent or unusable; inspect the output directory named in the message.

## `ARTIFACT_UNRESOLVED`

*Meaning:* the package build exited zero but the scaffold cannot tell which
application it produced (for example the forwarded arguments build a test target).
For `build`, this is a warning. For `build-run` and `sync-build-run` it is an error
and nothing was stopped, installed, or launched.

*Next:* run the application you built with `bdev run --artifact <path>`.

## `LAUNCH_FAILED`

*Meaning:* an existing instance would not exit, or the selected application did not
start. The error says which and lists the steps taken. Other processes are never
touched.

## Stale or unknown build freshness

`bdev run` may start an older output. `STALE_BUILD` means tracked inputs changed
since the recorded build. `UNKNOWN_FRESHNESS` means there is no record, or an
earlier rebuild of this output failed, was cancelled, or was interrupted, so its
contents may be partly overwritten. Rebuild with `bdev build` to refresh the
record; nothing rebuilds or deletes for you.

## Interrupted operations

Interrupting a command (Ctrl-C or SIGTERM) forwards the signal to its child
process group, waits briefly, then ends it, and reports `cancelled` with exit
`130` or `143`. An interrupted build leaves its output marked as needing
revalidation; a command killed outright leaves an operation record still marked
incomplete in `.bdev/operations/` beside your configuration. Inspect the checkout
before retrying anything that modifies it.
