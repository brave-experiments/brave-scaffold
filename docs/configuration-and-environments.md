# Configuration and environments

The repository includes a configuration template. The CLI, configuration reader,
and environment setup are not implemented yet; these files do not activate tools.

Copy `bdev.example.toml` to `bdev.toml` and replace its placeholder Core path with
the absolute path to your checkout's `src/brave` directory. `bdev.toml` is ignored
by Git. Keep machine paths out of the tracked example.

The initial configuration contract is:

| Field | Meaning |
| --- | --- |
| `schema_version` | Configuration format version; currently `1` |
| `logging.commands` | Full effective command and working-directory logging; defaults to `true` |
| `defaults.platform` | Optional platform override; absent means the current host platform |
| `checkouts[].alias` | Optional name for a checkout; does not select a default checkout |
| `checkouts[].core` | Absolute canonical path to that checkout's Core Git root |
| `checkouts[].direnv_dir` | Directory containing the checkout's `.envrc`, loaded through direnv before command execution; relative paths resolve beside `bdev.toml` |

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
