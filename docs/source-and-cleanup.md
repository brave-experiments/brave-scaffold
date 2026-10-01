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
bdev sync                       # macOS
bdev sync android --plan        # show the command; runs nothing
bdev sync mac,android --force   # extra arguments go to the sync script
```

`bdev sync [<targets>]` runs Core's `sync` script with the checkout-local tools.
Extra arguments go to that script only (in `sync-build`, only to the build phase).
Standalone `sync` reads `-C` as the script's own option, never as an output
directory. Mobile targets build `--target_os` from the union of the checkout's
existing `.gclient` values and the requested mobile target; the host platform is
never written there.

Before syncing, the command checks every repository the sync can reset. Core's sync runs gclient
with `--reset`, which discards edits to tracked files in Chromium and in each dependency repository
gclient manages. The command reads those repositories from gclient's own records
(`.gclient_entries` in the workspace and `.brave_gclient_entries` in Core) and stops with
`PREPARATION_CONFLICT` when

- Core has any uncommitted change, untracked files included;
- another repository has staged changes, even when its working file matches a
  recorded patch or sync result;
- a repository has untracked files. Incoming revision paths are unknown before
  sync, so the guard also blocks files that might turn out to be unrelated;
- a tracked file in Chromium or in a dependency repository differs from `HEAD`, unless it still holds
  exactly what patch or Android support preparation last wrote there, or what Core's tools had left
  when the last sync finished;
- patch application after the sync could overwrite local Chromium edits (see below);
- either records file is missing or unreadable, or Git cannot inspect a repository completely.

Core's own tools change many tracked Chromium files besides patch targets (translations, images,
the version file). After each successful sync the command records the tracked changes present, so those
files count as Core's output until they change again. A checkout that was synced before the scaffold
existed has no such record; the first sync then lists every such file. Review the list (`git status`
in the named repository, `bdev drift`) and, only if none of it is your work, repeat with
`--adopt-local-changes` to record it. An agent must not pass that option without your approval.

A hard reset can replace an untracked file or directory that obstructs an incoming
tracked path. Save untracked work outside the affected repositories before syncing;
the command does not stash or remove it. The operation record
notes Core and Chromium revisions before and after. Sync changes source and
dependencies inside the checkout; it is the requested work, not an installation
side effect.

Options that delete unused dependencies or unversioned trees stop before dispatch:
the guard cannot establish their complete deletion scope. Repeat without those
options after saving wanted work; scaffold does not discard it.

Android additions: `bdev sync android` requires nothing beyond the existing
`.gclient`; the mobile union is described above. The Android support working copy
and its refresh are described in [Android](android.md#android-on-mac-support-repository);
a refresh that would overwrite local Chromium edits stops with `PREPARATION_CONFLICT`
like patch preparation does.

## Patch preparation

Builds and tests apply Core patches only when the materialized Chromium files no
longer match the patch metadata, and only when that cannot lose local work.

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
metadata or the scaffold's receipt recorded for it. Otherwise it may hold local work,
and the command stops with `PREPARATION_CONFLICT`, lists the files, and suggests
`bdev drift --diff`. That covers a file that differs from its metadata and from the
receipt, a file with no earlier record, and a target a patch gained that has staged
changes, unstaged edits, a deletion, a rename, or an untracked file at its path. A
target nothing claims and Git shows as unchanged does not block. If Git cannot answer,
a patch's targets cannot be read, or a patch's metadata is unusable (unreadable, another
schema version, or any entry without a valid relative path and checksum), the command
stops as well; one bad entry makes that patch's whole metadata untrusted.

The same checks cover Core's version update: `chrome/VERSION` and
`chrome/VERSION.chromium`. An existing untracked version or sidecar needs a matching
generated-content receipt, including when Git ignores it. Plans list both files.

Resolve a conflict yourself: keep wanted edits with `bdev patches update` or
restore the files, then run `bpm run apply_patches` if you want stale files
replaced. The receipts live beside your configuration in `.bdev/`, never in Core.

## Inspect drift

```sh
bdev drift          # which patched Chromium files differ from their metadata
bdev drift --diff   # include each file's Git diff
```

Read-only. Reasons are `source changed after patch applied`, `source file missing`,
`patch file changed`, and `patch file removed`. It covers every repository in
`patches/.repositories.cfg`, and reports paths relative to Chromium's source root. Patches without
metadata, unusable metadata (including any entry without a valid path and checksum), or
an unreadable repository list make the evidence incomplete, and the result says so
instead of reporting a clean tree.

## Update patches

```sh
bdev patches update
```

Runs `bpm run update_patches` to regenerate patch files from local Chromium edits,
then lists the changed files in Core for review. It never commits or discards
anything.

## Clean generated build output

Prerequisites: a registered or enclosing checkout. Stop any build first; only one
operator may use a checkout at a time and the scaffold does not lock it.

```sh
bdev clean                                   # preview for the default target
bdev clean android --configuration debug     # preview one target and configuration
bdev clean mac --arch arm64 --execute        # delete the previewed directories
bdev clean all --execute                     # explicitly mac and android
```

- The target is `mac`, `android`, or `all`. Omitting it selects the default
  target (explicit target, then `defaults.platform`, then the host). Omission
  never means all.
- `--configuration debug|release|all` (default `all`) and `--arch <arch>` narrow
  the match. iOS is not available.
- Only directories directly under the selected checkout's own `src/out` are
  considered: macOS `Debug_arm64`, `Release_arm64`, the `Origin` variants, and the
  unsuffixed base; Android `android_Debug_arm64`, `android_tests_Debug_arm64`, and
  the analogous names. Other directories and other checkouts are never scanned.

Preview writes nothing:

```text
Preview (nothing is deleted) in /work/browser/_bad_scm/workspace/src/out
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
entry carries its outcome (`deleted`, `skipped`, `failed`) and reason.
`--no-size` skips size calculation.

If `src/out` itself is a symlink or resolves elsewhere, nothing is deleted
(`OWNERSHIP_CONFLICT`).

## Interruption

Interrupting cleanup (Ctrl-C or SIGTERM), or a failure part way, can leave a directory partly
removed. Nothing is restored and nothing resumes by itself. Before it moves a directory
aside, cleanup saves its private name and identity in the operation record (step `delete`,
in `.bdev/operations/`), so the record still names the remainder if the process dies. A
cancelled run reports the directory as `interrupted` with its private name, and records
what remains under `details.remaining`. Failed and interrupted deletions keep their
ownership record while the private directory exists, even after later operations
exceed log retention. Once the remainder is gone, normal pruning can remove that
record.

The next `bdev clean` finds the remainder. A directory with a `.scaffold-deleting-*` name
counts as a remainder only when a record from this checkout names it and it is still the
directory that record identified (same device and inode). It is listed as `planned` with
"unfinished deletion of <output> (operation <id>)", for the targets you select, and
`--execute` continues removing exactly that directory. Any other directory with a
cleanup-style name is listed as `skipped` and left alone, and so is a recorded name that has
since been replaced. A partly removed output is not a usable build; rebuild before running it.
