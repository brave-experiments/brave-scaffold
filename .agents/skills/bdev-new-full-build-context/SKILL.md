---
name: bdev-new-full-build-context
description: Build Brave for macOS or Android and run tests directly changed by the selected branch or working tree through this repository's bdev commands. Use for app builds, changed-test execution, combined build/test work, and Android-on-macOS test preparation while preserving the local Core test overlay.
---

# Brave full build context

Use this as an execution skill for the user's requested build or test work.
Resolve `SCAFFOLD_ROOT` to the repository containing this skill, following
symlinks. Call its absolute `scripts/bdev` path; no global installation or shell
activation is needed. Read [agent workflows](../../../docs/agent-workflows.md)
for execution authority and reporting.

An explicit request to use this skill to build or test authorizes those operations
on the selected checkout, including their normal guarded source preparation.
Automatic skill selection, reading this skill, and requests to explain or plan
commands grant no execution authority. Continue within existing authorization;
do not ask again merely because a build takes time or needs its normal preparation.
Sync, output cleanup, tool repair, support setup or branch switching, and app
launch or deployment require a request covering those actions. Only the user runs
`direnv allow`.

## Select the checkout and scope

- Use the checkout named by the user, otherwise let `bdev` infer it from cwd.
  Outside a checkout, inspect `bdev checkout list --json` and resolve a precise
  selection. Ask when the intended checkout is unclear. Checkout paths come from
  local configuration and may be outside this repository; do not derive an alias
  from directory naming conventions.
- Pass `--checkout <name-or-path>` once selected. Establish that no other operator
  is using it before build or test writes; do not infer this from an empty process
  list. Honor an existing agreement without asking again.
- Honor the requested platform. For an app build with no platform choice, use
  the host platform. For changed tests, use the discovery result's platforms;
  restrict to the user's platform when supplied.
- Inspect the selected checkout with `bdev doctor <platform> --json`. Readiness
  failures and suggested repairs do not authorize setup or source changes beyond
  the requested work.

| Request | Action |
| --- | --- |
| Build the app | Run `bdev build` for the selected platform. |
| Run changed tests | Discover and execute mapped suites with `bdev test-local`. |
| Build the app and test branch changes | Build the selected platform, then run its mapped changed tests. |
| Run a named suite or filter | Use `bdev test`; it need not appear in the branch diff. |
| Show a plan | Use `--plan`; do not execute the resulting commands. |

## Build

Use the scaffold's build commands with Debug arm64 and remote execution by
default, unless the user specifies otherwise:

```sh
"$SCAFFOLD_ROOT/scripts/bdev" --checkout <checkout> build mac --json
"$SCAFFOLD_ROOT/scripts/bdev" --checkout <checkout> build android --json
```

Use `--configuration release`, `--target_arch=<arch>`, or other forwarded package
options when requested. Keep remote execution enabled unless local compilation was requested;
`--offline` selects local compilation. Normal Android builds perform the required
support refresh after their checks pass. See [macOS](../../../docs/macos.md) and
[Android](../../../docs/android.md) for prerequisites, output selection, and
preparation writes.

Use `build-run` only when the user also requests launching or deploying the app.
Use the recorded artifact from the effective build output; an unresolved or failed
build does not justify running an older default artifact. After a failure, inspect
the diagnostic log and report the failing phase before choosing a recovery step.
Do not clean, reset, sync, or rerun an unchanged failing command as an implicit fix.

## Directly modified tests

Use the built-in discovery and runner; do not recreate test mapping in a skill
helper. Start with a read-only discovery plan:

```sh
"$SCAFFOLD_ROOT/scripts/bdev" --checkout <checkout> test-local --base origin/master --scope both --plan --json
```

`both` includes committed, staged, unstaged, and untracked changes. Honor a supplied
base or scope. Otherwise use `origin/master` when it exists locally, then `master`.
If neither exists, use `--base HEAD --scope worktree` and report that committed
changes were not considered. Even worktree-only discovery requires a valid base.
Do not fetch or change refs merely to discover tests.

Read `data.discovery`, including its phases, filters, and unmapped files. Execute
the same command without `--plan` for authorized test work; a plan alone does not
fulfill a request to run tests. Add `mac` or `android` after `test-local` when the
user limits the platform. If nothing maps, report that nothing ran rather than
substituting an unrelated broad suite.

Current mappings include Java `junit` tests to `brave_junit_tests`, Java
`javatests` to `brave_java_unit_tests`, desktop C++ unit tests to
`brave_unit_tests`, and desktop C++ browser/UI tests to `brave_browser_tests`.
Desktop WebUI tests map through their registered C++ harness. Trust the command's
mapping and exclusions; see [test-local](../../../docs/commands.md#test-local).

For a named test, use the suite and filter directly:

```sh
"$SCAFFOLD_ROOT/scripts/bdev" --checkout <checkout> test mac brave_unit_tests --filter 'Example.*' --json
"$SCAFFOLD_ROOT/scripts/bdev" --checkout <checkout> test android brave_junit_tests --filter '*ExampleTest*' --json
"$SCAFFOLD_ROOT/scripts/bdev" --checkout <checkout> test android brave_java_unit_tests --filter 'ExampleTest.*' --device <serial> --json
```

Core's test command builds the suite before running it. JUnit runs on the host and
must not receive `--device`; use a fully qualified class or wildcard filter.
Device Java tests use the requested serial, configured default, or sole usable
device. Resolve ambiguity before execution; never pick the first device.

`test-local` checks Android support and device requirements before running any
phase. If those block a mixed plan, run authorized independent macOS phases with
`test-local mac`. If only the device is missing, execute the discovered host-side
JUnit phase with `bdev test android brave_junit_tests` and its exact filter.
Report the device-backed phases as blocked. Do not rerun phases already completed.

## Android support and Core overlay

Read [Android tests](../../../docs/android.md#tests) before Android test execution.
Each checkout has its own support working copy at
`<workspace>/brave-android-mac-support`. Tests currently require its branch to be
exactly `android-testing-prototype`; another branch or detached HEAD blocks them.
Report the actual path and branch. Do not switch it or create a support working
copy without authorization covering that change.

Let `bdev test` prepare the reviewed support scripts and Core test overlay. The
overlay currently affects these paths relative to Core:

```text
build/commands/lib/androidTestMacHost.ts
build/commands/lib/androidTestMacHost.test.ts
build/commands/scripts/test.ts
```

Preserve existing overlay content and staging. Keep it out of feature commits;
do not reset, reverse, stage, unstage, or commit it as incidental cleanup. An
already applied overlay is expected. A partial or conflicting overlay, unknown
script identity, or unknown write scope is a real blocker: inspect and report it
without forcing patches or adding unreviewed hashes. Ordinary branch test fixes
outside the overlay remain product work.

## Report

State the checkout, platform, commands run, build artifact when verified, and
per-suite outcomes. Read JSON `status`, `error.code`, `exit_code`,
`child_exit_code`, warnings, and test counts; include diagnostic log paths for
failures. Distinguish verified passing tests from a zero exit with missing
results. List unmapped, skipped, blocked, and unrun work with reasons. A completed
build is not evidence of test execution or deployment.
