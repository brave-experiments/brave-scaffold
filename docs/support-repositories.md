# Support repositories

`scripts/sync-support-repos` clones and updates the repositories listed in the root `support-repos.toml`. Edit that file to add or remove repositories, change a source URL, or choose a branch. Each entry has `name`, `url`, and `branch`; the repository goes into `support/<name>`.

The command works from any current directory and needs no other configuration file. Supply Git credentials before syncing; it disables terminal authentication prompts.

## Commands

| Command | Behavior |
| --- | --- |
| `scripts/sync-support-repos` | Clone missing repositories and fast-forward existing ones |
| `scripts/sync-support-repos --repo handbook` | Operate on one manifest entry |
| `scripts/sync-support-repos --status` | Show branches, revisions, and local changes without fetching |
| `scripts/sync-support-repos --prune` | Preview removal of repositories absent from the manifest |
| `scripts/sync-support-repos --prune --execute` | Remove eligible repositories absent from the manifest |
| `scripts/sync-support-repos --discard-local --execute --repo handbook` | Reset local commits and tracked files to the fetched manifest branch |

Output is plain text. Use `--quiet` to hide progress and child output, or `--verbose` to show Git probes. Commands save redacted diagnostics in `.bcore/logs/` and print the log path.

## Local work

Normal sync skips changed or untracked files, a different branch or origin URL, and history that cannot fast-forward. It never switches branches. Fast-forward updates refuse to overwrite ignored local files. New clones use full history; existing shallow clones are not deepened automatically.

`--discard-local` requires `--execute`. It resets the current manifest branch and tracked files. Git may remove untracked files that obstruct tracked paths; other untracked and ignored files remain. It still refuses a different branch, origin URL, symlink, or nonrepository destination.

Prune preserves changed, untracked, and ignored files; stashes; detached HEADs; shallow history; linked worktrees; submodules; nested repositories; and commits absent from local remote references. Without remote references, it refuses removal. It does not fetch to check them. Ordinary directories and symlink repositories are left alone. `--repo` does not apply to prune.

Skipped sync or prune entries exit 6. Invalid input exits 2, ownership conflicts exit 4, and failed Git operations exit 5. Completed operations are printed as they finish. Failures and interruptions do not undo earlier operations or remove partial clones; inspect the affected repository before retrying.

The command uses the scaffold's Python runtime and is separate from `bcore`. Brave Scaffold is optional and requires no changes to Core's standalone workflow. This command does not install skills or write Core integration files, hooks, Git settings, or exclusions. `bcore android setup` separately manages each browser checkout's Android support working copy; syncing this collection does not update those working copies.
