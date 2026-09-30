# Configuration and environments

The repository includes a configuration template. The CLI, configuration reader,
and environment setup are not implemented yet; these files do not activate tools.

Copy `brave-scaffold.example.toml` to `brave-scaffold.toml` and replace its placeholder Core path with
the absolute path to your checkout's `src/brave` directory. `brave-scaffold.toml` is ignored
by Git. Keep machine paths out of the tracked example.

`brave-scaffold.toml` is the project-level configuration for Brave Scaffold.
`bdev` is its main command, not the owner of the configuration file. Other
scaffold tools and Brave project integrations may add documented sections when
implemented. Do not add speculative fields or extra configuration layers now.

## Tooling interpreter

The planned tooling runtime is a standard-library virtual environment under
`scripts/.venv`. Create it explicitly from the scaffold root with Python 3.14 or
newer, without installing pip or other packages:

```sh
python3.14 -m venv --without-pip scripts/.venv
```

An absolute path to another compatible Python interpreter is also valid. Python
itself is a machine prerequisite; launchers and direnv activation will not install
it. Creating this environment does not implement or activate the pending CLI.

Launchers will invoke this installation's `scripts/.venv/bin/python` directly,
check its version, and ignore inherited Python home/search-path overrides for the
scaffold process. They will not fall back to a Python on PATH or require manual
activation. Checkout-local `vpython3` remains a separate runtime for browser work.

If the scaffold moves or the base interpreter disappears, recreate the
scaffold-owned virtual environment explicitly. Missing, broken, or incompatible
runtime errors must explain that repair; they must not change browser toolchains.

## Checkout configuration

The initial configuration contract is:

| Field | Meaning |
| --- | --- |
| `schema_version` | Configuration format version; currently `1` |
| `logging.commands` | Full effective command and working-directory logging; defaults to `true` |
| `defaults.platform` | Optional platform override; absent means the current host platform |
| `checkouts[].alias` | Optional name for a checkout; does not select a default checkout |
| `checkouts[].core` | Absolute canonical path to that checkout's Core Git root |
| `checkouts[].direnv_dir` | Directory containing the checkout's `.envrc`, loaded through direnv before command execution; relative paths resolve beside `brave-scaffold.toml` |

Add another `[[checkouts]]` table for each checkout. Each checkout needs its own
Core path and environment directory. Store Core's path once; surrounding source
paths will be derived and validated by the CLI.

Checkout selection will use an explicit selector or the current working
directory. If neither identifies one checkout, the caller must supply a precise
location. An alias named `main` does not change that rule.

An environment mapping is configuration only. It does not create an `.envrc`,
load it, or approve it. Environment setup will generate files outside Core and
show their contents for review before the user runs `direnv allow`. Do not copy
activation files into Core or change shell configuration to prepare these files.

General support-repository and skill catalog configuration is not defined here.
Add those fields with the corresponding feature and its validation rather than
inventing fields that no configuration reader supports.
