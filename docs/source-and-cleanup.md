# Source and cleanup

Source changes (synced dependencies, applied patches, edited files) live in the
checkout's repositories. Generated outputs are the build directories under
`<chromium-src>/out`. They are separate: cleaning removes outputs only and never
touches source.

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

Untracked files in dependency repositories are not listed because a reset does not remove them. The
command never stashes, resets, or switches branches. The operation record
notes Core and Chromium revisions before and after. Sync changes source and
dependencies inside the checkout; it is the requested work, not an installation
side effect.

Android additions: `bdev sync android` requires nothing beyond the existing
`.gclient`; the mobile union is described above. The Android support working copy
and its refresh are described in [Android](android.md#android-on-mac-support-repository);
a refresh that would overwrite local Chromium edits stops with `PREPARATION_CONFLICT`
like patch preparation does.

## Patch preparation

Builds and tests apply Core patches only when the materialized Chromium files no
longer match the patch metadata, and only when that cannot lose local work. Each
patched file's checksum is compared with its metadata and with the state recorded
the last time patches were applied here. A file that differs from both, or that
differs with no earlier record, may hold local edits: the command stops with
`PREPARATION_CONFLICT`, lists the files, and suggests `bdev drift --diff`. Missing
or unreadable metadata is treated as uncertain, not clean.

A patch that has no metadata yet cannot be compared that way, so its targets (read from
the patch headers, whatever the prefix) are checked directly: a target with staged
changes, unstaged edits, a deletion, a rename, or an untracked file at its path stops the
command the same way, before anything is applied. If Git cannot answer, or the patch's
targets cannot be read, the command stops as well.

Resolve a conflict yourself: keep wanted edits with `bdev patches update` or
restore the files, then run `bpm run apply_patches` if you want stale files
replaced. The receipts live beside your configuration in `.bdev/`, never in Core.

## Inspect drift

```sh
bdev drift          # which patched Chromium files differ from their metadata
bdev drift --diff   # include each file's Git diff
```

Read-only. Reasons are `source changed after patch applied`, `source file missing`,
`patch file changed`, and `patch file removed`. Patches without metadata or
unreadable metadata make the evidence incomplete, and the result says so instead of
reporting a clean tree.

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

Interrupting cleanup (Ctrl-C or SIGTERM) can leave a directory partly removed.
Nothing is restored. Rerun `bdev clean` to preview what remains, then delete or
rebuild it. A partly removed output is not a usable build; rebuild before running
it.
