# Troubleshooting

Each entry gives a symptom, what it means, and the next step. Steps marked **you** need a person; steps marked **changes files** modify something and should be run only when you intend that. Nothing here recommends a global fallback or automatic approval.

## The launcher reports a missing or broken runtime

*Symptom:* `the scaffold Python runtime is missing or broken` (exit 3) or a message that the runtime is older than Python 3.14.

*Meaning:* `scripts/.venv` does not exist, points at a removed Python, or was built from an older one. Launchers never fall back to another Python.

*Next (changes files, scaffold-owned only):* `python3.14 -m venv --clear --without-pip scripts/.venv`. Checkouts and their tools are untouched.

## `CHECKOUT_REQUIRED` or `CHECKOUT_AMBIGUOUS`

*Meaning:* your current directory is not inside one checkout, or the path holds several source workspaces. Nothing is guessed.

*Next:* run `bcore checkout list`, then repeat with `--checkout <alias-or-path-to-src/brave>`.

## `UNSUPPORTED_CAPABILITY` about Git linked worktrees

*Meaning:* Core, Chromium, or the outer checkout is a Git linked worktree.

*Next:* select an existing full checkout. The scaffold does not convert, move, or delete anything.

## `ENVIRONMENT_REQUIRED`

*Meaning:* the checkout has no configured environment, the file is missing, or direnv is not installed.

*Next (changes files in the scaffold):* `bcore env init --checkout <name>`. Install direnv if it is missing (**you**).

## `ENVIRONMENT_UNAPPROVED`

*Meaning:* direnv has not approved the exact contents of the configured `.envrc`, either because it is new or because it changed.

*Next (**you**):* read the file named in the error, then run `direnv allow <directory>`. Agents must not do this.

## `ENVIRONMENT_LOAD_FAILED`

*Meaning:* the approved environment ran but failed. The error includes direnv's stderr.

*Next:* reproduce with `direnv exec <environment-dir> true`. If the file is the generated one, check that the scaffold path in it still exists; after moving the scaffold, run `bcore env init` again and approve the new file (**you**).

## `CHECKOUT_ENV_CONFLICT`

*Meaning:* the loaded environment names a different checkout than the one selected, for example a hand-edited `.envrc` exporting another `BRAVE_CORE_DIR`. The mismatched variables are listed.

*Next:* fix or regenerate the environment (`bcore env init`), then approve it (**you**).

## `LOCAL_TOOL_MISSING`

*Meaning:* a checkout-local tool is missing, stale, or outside the version range declared in `package.json`. The message names which. A global Node or pnpm never satisfies this and never causes a failure by itself.

*Next (changes files in the checkout):* `bcore tools setup --checkout <name>` runs the checkout's payload installer. Run it only when the checkout may change. `bcore doctor` and `bcore context` show the same checks without changing anything. If local `vpython3` is missing, follow the manual depot_tools restoration in [tool layouts](configuration-and-environments.md#supported-tool-layout); sync and tools setup need that interpreter and cannot restore it through the broken environment.

A tool that resolves (through links, including a linked payload directory or ancestor) outside the checkout is treated as missing, and so is a checkout with no local `vpython3` even when the approved environment names one elsewhere.

If the message says the pinned payload cannot be verified, the checkout's payload metadata (`tools/cr/extra_deps.py`, or a literal versioned npm archive entry in `tools/cr/install_extra_deps.py`) is missing or unreadable. A compatible version number alone does not prove the pinned payload, so the command stops rather than running unverified tools. Node is verified for every checkout; pnpm additionally for pnpm checkouts (an older npm checkout is never judged by pnpm's metadata). If the checkout has a payload installer the message names `bcore tools setup`; otherwise update the checkout to a revision that carries the metadata.

## `DEPENDENCY_INCOMPATIBLE`

*Meaning:* `package.json` declares an unsupported or malformed package manager. Only `npm` and `pnpm` are supported.

*Next:* inspect the file named in the error. The scaffold does not edit it.

## `READINESS_BLOCKED` and `READINESS_INCOMPLETE`

*Meaning:* `bcore doctor` found a required check that fails (`BLOCKED`) or could not be evaluated (`INCOMPLETE`). Both exit 3. The failing checks are listed with their repairs.

*Next:* handle blockers first. `not_checked` usually means no checkout was selected; add `--checkout`.

## `CHILD_FAILED`

*Meaning:* the package command or Python program ran and exited nonzero. The result's `child_exit_code` is its own status; the tools exit `5`. Its output is on your terminal (stderr in JSON mode). Quiet mode shows the failure's last 40 lines, up to 16 KiB. The final stderr line gives the diagnostic log path for the full saved output and command trace; see [output controls](commands.md#common-behavior).

## `CONFIG_INVALID`

*Meaning:* `brave-scaffold.toml` has an unknown field, wrong type, or a duplicate. The message names the field and shows a valid example.

## `PREPARATION_CONFLICT`

*Meaning:* build or test preparation could overwrite local work: patch application or a version update would touch a file that differs from its recorded state, or Android support preparation threatens local changes. The message lists the files and operation. A guard failure before preparation leaves files alone. A failure after scripts run can leave changes behind; inspect the checkout before retrying.

*Next:* review with `bcore drift --diff`. Keep wanted edits with `bcore patches update`, or restore the files yourself; then repeat the command. `--skip-support-refresh` blocks a needed Android refresh; omit it only if you want support files replaced. The scaffold never stashes or discards edits as an automatic repair.

Source sync runs Core's own command, which may reset repositories and overwrite local work. Core failures appear as `CHILD_FAILED`; see [sync behavior](source-and-cleanup.md#sync-sources).

## `SELECTOR_CONFLICT`

*Meaning:* an explicit scaffold choice disagrees with a forwarded argument (for example `--configuration release` and a forwarded `Debug`), or an option was given twice with different values. Both values and an example are shown; nothing ran.

## `ARTIFACT_MISSING`, `ARTIFACT_MISMATCH`, `ARTIFACT_AMBIGUOUS`

*Meaning:* `run` found no application for the checkout, target, configuration and architecture (`MISSING`), found one that is not a usable Brave application (`MISMATCH`), or found several (`AMBIGUOUS`). `run` never builds.

*Next:* `bcore build` or `bcore build-run`; or choose with `--artifact <path>`. After a build, `MISSING` or `MISMATCH` means the build succeeded but its expected output is absent or unusable; inspect the output directory named in the message.

## `ARTIFACT_UNRESOLVED`

*Meaning:* the package build exited zero but the scaffold cannot tell which application it produced (for example the forwarded arguments build a test target, or only prepare the build with `--prepare_only` or `--xcode_gen`). For `build`, this is a warning. For `build-run` and `sync-build-run` it is an error and nothing was stopped, installed, or launched.

*Next:* run the application you built with `bcore run --artifact <path>`.

## `LAUNCH_FAILED`

*Meaning:* an existing instance would not exit, or the selected application did not start. The error says which and lists the steps taken. Other processes are never touched.

## `DEPENDENCY_INCOMPATIBLE` for the Android support repository

*Meaning:* the checkout's support working copy is missing, or its version gate rejects this checkout (the reason is quoted). Nothing was prepared or built.

*Next:* `bcore android setup` creates a missing working copy (network). For a mismatch, pick a revision for this checkout only: `bcore android setup --ref <ref>`, which switches a clean working copy, or switch it yourself. A working copy with local changes or unpushed commits is never switched for you.

## `DEPENDENCY_INCOMPATIBLE` for the Android test branch

*Meaning:* `bcore test android` needs the support working copy on the `android-testing-prototype` branch, and it is on another branch or a detached HEAD. Nothing was switched, prepared, or built.

*Next:* switch it yourself: `git -C <working copy> switch android-testing-prototype` (the message gives the path). The scaffold never switches it.

## `PREPARATION_CONFLICT` for the Android test overlay

*Meaning:* Core has a partial or conflicting test overlay, the overlay patch writes files outside its reviewed list, or applying it changed other tracked files. Nothing was forced.

*Next:* review `git -C <src>/brave status --short`. Reverse a partial overlay with `./applyBraveCoreTestSupport.sh --src-root <src> --reverse` from the support working copy once you have kept any wanted edits, then repeat the test.

## `TEST_FAILED`, `NO_TESTS_RAN`, and `TEST_RESULTS_UNVERIFIED`

*Meaning:* the Android test command exited 0 but the results file shows failed tests (`TEST_FAILED`) or no tests (`NO_TESTS_RAN`), or no readable results file was written (`TEST_RESULTS_UNVERIFIED`, a warning: the count is unverified). A nonzero runner exit is `CHILD_FAILED`.

*Next:* read the saved log and `scaffold_test_results.json` in the test output directory. For `NO_TESTS_RAN`, fix the filter: `brave_junit_tests` needs a fully qualified class or a wildcard such as `*SomeTest*`.

## `DEVICE_AMBIGUOUS` and `DEVICE_UNAVAILABLE`

*Meaning:* several usable Android devices are connected (choose one), or the chosen device is missing, `offline`, or `unauthorized`. The ids and states are listed.

*Next:* `adb devices`, then pass `--device <id>` (or set `defaults.android_device`). For `unauthorized`, accept the USB debugging prompt on the device; for `offline`, reconnect it.

## Running an older output

`bcore run` and `bcore deploy` use the selected artifact without checking whether sources changed since the build. Run `bcore build` when you want a new build. Launching an artifact does not repair or clear records left by a failed or interrupted build.

## Interrupted operations

Interrupting a command (Ctrl-C or SIGTERM) forwards the same signal to the process group it started, waits up to 10 seconds for every member (including descendants whose parent already exited) to leave, then kills the ones that remain, and reports `cancelled` with exit `130` or `143`. If a started process could not be confirmed gone, or a probe's output pipes were abandoned because a process outside the group still held them, the result carries a `CLEANUP_INCOMPLETE` warning and `error.details.cleanup_incomplete`; look for leftover processes before retrying. Only processes this command started in its own group are signalled. An interrupted build leaves its output marked as needing revalidation; a command killed outright leaves an operation record still marked incomplete in `.bcore/operations/` beside your configuration. Inspect the checkout before retrying anything that modifies it.

## Support repositories are skipped

Run `scripts/sync-support-repos --status` and inspect each repository's path, branch, and local state. Sync skips local changes, a different branch or origin URL, and history that cannot fast-forward. Prune also preserves ignored files, stashes, local commits, shallow history, and linked or nested repositories. A skipped sync or prune exits 6. Inspect the repository before choosing an explicit reset or removal; a warning grants no authority to discard work. See [support repositories](support-repositories.md) for the command contracts.
