# Source and cleanup

Source changes (synced dependencies, applied patches, edited files) live in the
checkout's repositories. Generated outputs are the build directories under
`<chromium-src>/out`. They are separate: cleaning removes outputs only and never
touches source.

Unreadable patch directories make the inventory incomplete and block preparation,
even when another repository has valid metadata. An absent or empty patch directory
is supported; permission and I/O errors are reported.

## Sync sources

```sh
bcore sync                       # macOS
bcore sync android --plan        # show the command; runs nothing
bcore sync mac,android --force   # extra arguments go to the sync script
bcore sync ios                   # adds ios to target_os; Core's hooks bootstrap the project
```

`bcore sync [<targets>]` runs Core's `sync` script with the checkout-local tools.
Extra arguments go to that script only. In `sync-build` and `sync-build-run` they go to the build phase;
use `--sync-arg` for the sync phase.
Standalone `sync` reads `-C` as the script's own option, never as an output
directory. Mobile targets build `--target_os` from the union of the checkout's
existing `.gclient` values and the requested mobile target; the host platform is
never written there.

Core owns sync behavior, including package installation, repository updates and
resets, patch application, and hooks. Scaffold runs the checkout's package
`sync` script with its local Node and package manager and streams its output.
The same applies to the sync phase of `sync-build` and `sync-build-run`.

Sync can overwrite local changes according to Core's commands and the options
you pass. Save any work you need before running it. Scaffold does not inspect
upstream script hashes, predict hook writes, prompt for overwrites, back up
changes, or restore files before dispatch. Core and gclient report their own
conflicts and failures. Options such as `--force`, `--nohooks`,
`--sync_chromium=false`, and dependency deletion options go to Core unchanged.

`--plan` shows the command, working directory, prerequisites, and general checkout
writes without running sync. Core decides which repositories and files to change
when the command runs; the plan does not predict that decision or certify that
local work will survive.

A nonzero child exit stops the operation, including any later build or launch.
Scaffold records the child exit status and leaves partial changes as Core left
them. Successful results include the dispatched command and the Core and Chromium
revisions before and after sync.

Sync writes inside the checkout are the requested browser operation; scaffold
setup remains external and optional. Core's supported standalone workflow stays
available without scaffold.

iOS additions: `bcore sync ios` requires Core's hooks, so `--nohooks` is refused. After the
sync it checks the files Core's `bootstrap_ios` hook creates ([iOS](ios.md#sync-and-bootstrap)) and
stops with `PREPARATION_CONFLICT` and a repair suggestion when they are missing.

Android additions: `bcore sync android` requires nothing beyond the existing
`.gclient`; the mobile union is described above. The Android support working copy
and its refresh are described in [Android](android.md#android-on-mac-support-repository);
a refresh that would overwrite local Chromium edits stops with `PREPARATION_CONFLICT`
like patch preparation does.

## Patch preparation

Before compiling, builds and tests check Core's patch state and run
`apply_patches` when preparation is needed, and only when that cannot lose local
work. Combined commands use the same check; commands that sync first check after
sync. `--plan` previews the decision without applying patches.

Preparation is needed when patched files differ from their metadata, metadata is
incomplete, or patch/rewrite inputs have local changes. If patched files match
complete metadata and those inputs are clean, patch application is skipped.
Unusable metadata or local work that could be lost blocks preparation instead.

Core lists the repositories it patches in `patches/.repositories.cfg` (`//` is
Chromium itself; entries such as `//v8` or `//third_party/ffmpeg` are separate Git
repositories under it). Each repository's patches live in the matching directory
under `patches/`, and a patch's metadata paths are relative to its repository. The
scaffold reads that list, so nested repositories are checked and reported like
Chromium, with paths relative to Chromium's source root (`v8/BUILD.gn`). Patch files in
subdirectories with no repository list are incomplete evidence, not an empty result.

Applying a patch resets every file it targets, so the command checks the whole write
set of each patch Core would apply again: the files the current patch changes and the
files its earlier metadata recorded. Staged changes block preparation independently
of working-file content. Otherwise, a file is safe when it still holds content the
metadata or the scaffold's receipt recorded for it and has the executable bit recorded for it
(the receipt keeps each patched file's bit; an older receipt falls back to the bit Git has), because a
local `chmod` would be reset along with the contents. Otherwise it may hold local work,
and the command stops with `PREPARATION_CONFLICT`, lists the files, and suggests
`bcore drift --diff`. That covers a file that differs from its metadata and from the
receipt, a file with no earlier record, and a target a patch gained that has staged
changes, unstaged edits, a deletion, a rename, or an untracked file at its path. A
target nothing claims and Git shows as unchanged does not block. If Git cannot answer,
a patch's targets cannot be read, or a patch's metadata is unusable (unreadable, another
schema version, or any entry without a valid relative path and checksum), the command
stops as well; one bad entry makes that patch's whole metadata untrusted.

The same checks cover Core's version update: `chrome/VERSION` and
`chrome/VERSION.chromium`. An existing untracked version or sidecar needs a matching
generated-content receipt, including when Git ignores it. Plans list both files.

Resolve a conflict yourself: keep wanted edits with `bcore patches update` or
restore the files, then run `bpm run apply_patches` if you want stale files
replaced. The receipts live beside your configuration in `.bcore/`, never in Core.

## Inspect drift

```sh
bcore drift          # which patched Chromium files differ from their metadata
bcore drift --diff   # include each file's Git diff
```

Read-only. Reasons are `source changed after patch applied`, `source file missing`,
`patch file changed`, and `patch file removed`. It covers every repository in
`patches/.repositories.cfg`, and reports paths relative to Chromium's source root. Patches without
metadata, unusable metadata (including any entry without a valid path and checksum), or
an unreadable repository list make the evidence incomplete, and the result says so
instead of reporting a clean tree.

## Update patches

```sh
bcore patches update
```

Runs `bpm run update_patches` to regenerate patch files from local Chromium edits,
then lists the changed files in Core for review. It never commits or discards
anything.

## Clean generated build output

Prerequisites: a registered or enclosing checkout. Stop any build first; only one
operator may use a checkout at a time and the scaffold does not lock it.

```sh
bcore clean                                   # preview for the default target
bcore clean android --configuration debug     # preview one target and configuration
bcore clean mac --arch arm64 --execute        # delete the previewed directories
bcore clean all --execute                     # explicitly mac, android, and ios
```

- The target is `mac`, `android`, `ios`, or `all`. Omitting it selects the default
  target (explicit target, then `defaults.platform`, then the host). Omission
  never means all.
  The output names the selected platforms and whether they came from an explicit
  argument, the configured default, or the host default. With an omitted target,
  it also shows how to preview every platform. JSON reports this in `targets` and
  `target_source` (`explicit`, `configured`, or `host`).
- `--configuration debug|release|all` (default `all`) and `--arch <arch>` narrow
  the match. Core names the x64 output without an architecture suffix, so an unsuffixed
  directory such as `Debug` is matched by `--arch x64` (or no `--arch`) and never by `--arch arm64`.
- Only directories directly under the selected checkout's own `src/out` are
  considered: macOS `Debug_arm64`, `Release_arm64`, the `Origin` variants, and the
  unsuffixed base; Android `android_Debug_arm64`, `android_tests_Debug_arm64`, and
  the analogous names; iOS `ios_Debug_arm64_simulator` (and the other `ios_<Configuration>` names Core's
  Xcode pre-action creates) and `ios_Debug_xcode_derived_data`, which holds the Xcode products. Other directories and other checkouts are never scanned.

Preview writes nothing:

```text
Preview (nothing is deleted) in /work/browser/_bad_scm/workspace/src/out
Platforms: mac (host default only)
Configurations: debug, release; architecture: all
Other platforms are not checked. To preview every platform, run 'bcore clean all'.
  planned      12.3 GiB  /work/browser/_bad_scm/workspace/src/out/Debug_arm64
  planned       9.8 GiB  /work/browser/_bad_scm/workspace/src/out/Release_arm64
Run again with --execute to delete the directories marked 'planned'.
```

`--execute` deletes only that plan. The plan records the identity (device and inode)
of `src/out` and of each listed directory. Each directory is checked again
immediately before removal: an entry that became a symlink, is a different directory
now living at the same path, or holds a `.git` entry is skipped and reported, and so
is every entry if `src/out` itself was replaced. The approved directory is held open
and renamed to a private `.scaffold-deleting-*` name inside `src/out` before its
contents are removed through open handles, so a replacement is never reopened by name
and a symlink is never followed. If the renamed entry turns out not to be the approved
one, it is renamed back and nothing is deleted. A deletion that fails part way reports
the private name of what remains. If some
directories are skipped or fail, the result is `partial` with exit code 6 and each
entry carries its outcome (`deleted`, `skipped`, `failed`) and reason. An unfinished deletion that
belongs to a target you did not select is listed as `unselected`; it is reported and left alone,
and it does not make the run partial.
`--no-size` skips size calculation.

If `src/out` itself is a symlink or resolves elsewhere, nothing is deleted
(`OWNERSHIP_CONFLICT`).

## Interruption

Interrupting cleanup (Ctrl-C or SIGTERM), or a failure part way, can leave a directory partly
removed. Nothing is restored and nothing resumes by itself. Before it moves a directory
aside, cleanup saves its private name and identity in the operation record (step `delete`,
in `.bcore/operations/`), so the record still names the remainder if the process dies. A
cancelled run reports the directory as `interrupted` with its private name, and records
what remains under `details.remaining`. Failed and interrupted deletions keep their
ownership record while the private directory exists, even after later operations
exceed log retention. Once the remainder is gone, normal pruning can remove that
record.

The next `bcore clean` finds the remainder. A directory with a `.scaffold-deleting-*` name
counts as a remainder only when a record from this checkout names it and it is still the
directory that record identified (same device and inode). It is listed as `planned` with
"unfinished deletion of <output> (operation <id>)", for the targets you select, and
`--execute` continues removing exactly that directory. Normal and resumed deletions
check for a `.git` entry through the held directory descriptor immediately before
removing contents. A repository added after planning or interruption is kept and
reported as skipped. Any other directory with a
cleanup-style name is listed as `skipped` and left alone, and so is a recorded name that has
since been replaced. A partly removed output is not a usable build; rebuild before running it.
