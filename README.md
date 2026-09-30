# Brave development scaffold

Local tools for building, testing, running, and inspecting Brave checkouts.
Browser checkouts and support repositories may live anywhere and are selected
through local configuration.

Brave Scaffold (`brave-scaffold`) is entirely supplementary to Brave Core
(`brave-core`). Using it is optional. Adopting or using the scaffold requires no
changes to Brave Core's source, configuration, or development workflow. Brave
Core's own supported commands remain usable without the scaffold.

Scaffold configuration and integration files stay outside Brave Core. Explicitly
requested builds, syncs, and source preparation still perform their normal
checkout writes; those are the requested work, not scaffold installation changes.

## What works today

Two commands, `bdev` and `bpm`, both under `scripts/`:

- `bdev` inspects checkouts, generates and checks their external direnv
  environments, reports readiness, and runs the checkout-local Python.
- `bpm` runs the checkout's own package manager with its own Node.js, never a
  global one.

Building, testing, running, syncing, and cleaning are not available yet. The
`bdev capabilities` command lists what is supported and what has been verified.

The initial platform scope is macOS arm64 hosts with existing Brave macOS and
Android checkouts. Fresh checkout creation, iOS, and guarded push are not part of
it.

## Quick start

1. Create the tooling runtime (Python 3.14 or newer, no packages):
   `python3.14 -m venv --without-pip scripts/.venv`
2. Register a checkout: `scripts/bdev checkout add main /path/to/src/brave`
3. Generate its environment: `scripts/bdev env init --checkout main`
4. Review the printed file, then approve it yourself with `direnv allow <dir>`.
5. Check readiness: `scripts/bdev doctor mac --checkout main`

Nothing requires a `PATH` change, a shell hook, or an edit inside Brave Core.

## Guides

| Task | Guide |
| --- | --- |
| Install and run a first command | [Getting started](docs/getting-started.md) |
| Configure checkouts and environments | [Configuration and environments](docs/configuration-and-environments.md) |
| Look up commands, options, results, exit codes | [Commands](docs/commands.md) |
| Sign commits | [Commit signing](docs/signing.md) |
| Diagnose a failure | [Troubleshooting](docs/troubleshooting.md) |
| Drive the tools from an agent | [Agent workflows](docs/agent-workflows.md) |
| Change or extend the tools | [Development](docs/development.md) |
| Project skills for contributors | [Skills](docs/skills.md) |

Read [AGENTS.md](AGENTS.md) before contributing. `CLAUDE.md` links to the same
instructions.

Licensed under the [Mozilla Public License 2.0](LICENSE).
