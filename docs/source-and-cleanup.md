# Source and cleanup

Source changes (synced dependencies, applied patches, edited files) live in the
checkout's repositories. Generated outputs are the build directories under
`<chromium-src>/out`. They are separate: cleaning removes outputs only and never
touches source.

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

`--execute` deletes only that plan. Each directory is checked again immediately
before removal; an entry that became a symlink, moved outside `src/out`, or holds
a `.git` entry is skipped and reported, and a symlink is never followed. If some
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
