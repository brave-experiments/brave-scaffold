# Getting started

Brave Scaffold is entirely supplementary to `brave-core`. Using it is optional, and setting it up requires no change to `brave-core`: no `.envrc`, hook, Git setting, or exclusion is written inside a checkout. `brave-core`'s own commands keep working as before.

## For agents

Read [AGENTS.md](../AGENTS.md) and [Agent workflows](agent-workflows.md) before starting. Use the setup steps below; there is no separate installation process for agents.

1. Inspect the developer's host, prerequisites, checkout paths, and existing shell configuration. Check for an existing Scaffold installation too. Keep their checkout locations, scripts, and aliases.
2. Explain what needs installing or configuring and which files would change. Ask about any missing choices and get approval before making changes, unless the developer has already authorized the setup.
3. Follow the steps below for the agreed setup. Reuse existing configuration and avoid adding duplicate shell entries. Setup must not change files inside `brave-core`.
4. Show the generated environment files and the `direnv allow` commands. The developer must review the files and run those commands themselves.
5. After approval, check readiness for the developer's target with `bcore doctor`. Explain any remaining problems and recommend a first command based on their workflow, such as `bcore test mac --plan` for changed tests. Do not start a build, test, sync, or browser launch unless the developer requested it.

## Prerequisites

| Need | Notes |
| --- | --- |
| macOS on arm64 | The supported host. Other hosts are unsupported. |
| Python 3.14 or newer | Only to create the tooling runtime. Install it yourself (for example with Homebrew); no launcher or hook installs it. |
| Git and [direnv](https://direnv.net/docs/installation.html) | Both must be on `PATH`. direnv's shell hook is optional. |
| An existing Brave checkout | A full checkout, not a Git linked worktree. |

## Install

Run these from the scaffold root:

```sh
python3.14 -m venv --without-pip scripts/.venv
scripts/bcore setup
```

The first command creates the scaffold-owned virtual environment. It installs no packages and needs no activation; launchers call `scripts/.venv/bin/python` directly and ignore `PYTHONHOME`, `PYTHONPATH`, and any active virtual environment. An absolute path to another compatible Python works too. If the scaffold moves or the base Python disappears, recreate only this environment with the command shown in the launcher's error message.

`scripts/bcore setup` prepares `brave-scaffold.toml` when it is missing and lists the next steps. It changes nothing inside `brave-core`.

To type `bcore` and `bpm` without a path and enable checkout switching, add these lines to your Bash or Zsh startup file, replacing `/path/to/brave-scaffold` with the absolute path to this repository. Reload the file or open a new shell:

```sh
export PATH="/path/to/brave-scaffold/scripts:$PATH"
source "/path/to/brave-scaffold/scripts/bcore-shell.sh"
```

This is optional; you can use `scripts/bcore` directly from the scaffold root.

To change directories with `bcore cd main` or `bcore cd alt-1`, also source `scripts/bcore-shell.sh` in your Bash or Zsh startup file. These commands enter the selected checkout's `src/brave` directory. A separate process cannot change your shell's directory, so running `scripts/bcore cd main` directly prints the path instead.

Scaffold and your checkouts do not need a shared parent directory. You can keep Scaffold in its own folder, with checkouts elsewhere or on different volumes. You can also use Scaffold as your development folder and nest checkouts inside it; keep those directories ignored by Scaffold's Git configuration. Setup must not add exclusions inside `brave-core`.

Configuration lives beside the Scaffold installation you run, regardless of your current directory, unless you pass `--config`. Each registered checkout needs its own approved external environment.

## Register a checkout

```sh
scripts/bcore checkout add main /work/browser/_bad_scm/workspace/src/brave
```

Give the `brave-core` directory, the Chromium `src` directory, or the outer checkout directory. The command records the canonical `brave-core` path under the alias `main` in `brave-scaffold.toml`. Use one full checkout per branch or workspace; Git linked worktrees are rejected.

## Create and approve the environment

```sh
scripts/bcore env init --checkout main
```

This writes `environments/main/.envrc` beside your configuration, prints its contents and the code it runs, and prints the approval command. Read the file, then approve it yourself:

```sh
direnv allow /path/printed/by/the/command
```

The scaffold never approves an environment. Any later change to the file needs a new approval. See [configuration and environments](configuration-and-environments.md).

## First commands

```sh
scripts/bcore doctor mac --checkout main     # readiness; changes nothing
scripts/bcore context --checkout main        # what the tools resolved
scripts/bpm --checkout main run --help      # checkout-local package manager
```

From inside the checkout, omit `--checkout`: the current directory identifies it. Outside a checkout, name one with `--checkout`.

To build and run the browser, continue with [macOS](macos.md).

If a command fails, its error names the problem and a next step. See [troubleshooting](troubleshooting.md) and [commands](commands.md).

Use one operator per checkout at a time. The scaffold does not lock checkouts.

For optional commit signing with 1Password, run `scripts/bcore doctor signing` and follow the [commit signing guide](signing.md).
