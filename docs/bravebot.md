# bravebot

`bbot` builds and runs release copies of a local [bravebot](https://github.com/brave/bravebot) checkout, the way `bcore` works with a Brave Core checkout. It is a separate launcher: it shares the scaffold's layout and local configuration, not `bcore`'s commands, records, or logs.

```sh
bbot build                 # build the terminal app from the checkout and install the copy
bbot build ui              # build and install the desktop app (macOS)
bbot br --help             # build the terminal app, then run it with arguments
bbot br ui                 # build the desktop app, then open it
bbot status                # installed builds and the checkout's current commit
bbot ui                    # open the installed desktop app
bbot --help                # any other arguments run the installed terminal app
bbot make <target>         # run make in the checkout
bbot cal                   # bbot make check-all-local
```

Only `build`, `br`, `make`, and `cal` build or run anything in the checkout. `status` only reports. `ui` and any other arguments run the installed copy and never rebuild; they warn when the checkout's `HEAD` no longer matches the build, or when the checkout has uncommitted changes the build may not include.

## Checkout and installed copies

The checkout is `bravebot_path` in `brave-scaffold.toml` ([fields](configuration-and-environments.md#fields)), else `bravebot` beside the scaffold's `scripts/` directory. The installed copies live in `.bbot/` in the scaffold directory, which Git ignores: `.bbot/bravebot` and `.bbot/ui/Brave Bot.app`, each with a `build-info` record naming its source directory, commit, whether the checkout had uncommitted changes, and when it was built. `.bbot/` is readable only by you, because the copies contain the credentials. The record is marked as installing before a copy is replaced, so an interrupted install is reported until the next build. While Brave Bot runs from `.bbot/ui`, `build ui` and `br ui` refuse to replace it; quit it first. The desktop app is installed only if the build wrote a new bundle for this Mac's architecture (Apple Silicon is detected even from a shell under Rosetta); an older bundle left in `ui/dist` is refused. The terminal app is copied from where `cargo` reports it built, which follows `CARGO_TARGET_DIR`; `cargo` runs inside the source, so its toolchain file and cargo configuration apply.

`--source <dir|name>` before the command builds from one of the checkout's Git worktrees instead: a name under `<checkout>/.worktrees` (tried first for a value without a `/` or a leading `.`), or a directory. `--source` applies to `build`, `br`, `make`, `cal`, and `status`; other commands refuse it. Without it, those commands use `BBOT_SOURCE` (resolved the same way), else the worktree of the checkout that contains the current directory, else the checkout itself. Worktrees kept under `<checkout>/.worktrees` do not count as uncommitted changes in the checkout. A directory that is not the top level of a worktree of the checkout's repository is refused. There is one installed copy of each app, so building from another worktree replaces it.

## Credentials

The backend credentials are compiled into the builds. They come from the checkout's own `.envrc`, which only you approve with `direnv allow`, so every build, `make`, and `cal` runs through `direnv exec` in the checkout. Without `direnv`, without a `.envrc`, or with an environment that is not approved, `bbot` stops before building instead of building without the credentials. The terminal app is built with `cargo build --release --locked` and `BRAVEBOT_ALLOW_UNCONFIGURED_BUILD=0`, set inside the loaded environment, so bravebot's own build check refuses missing credentials even when your shell or `.envrc` allows unconfigured builds. If the source changes while a build runs, its record is marked as built from uncommitted changes. Building from a worktree still loads the checkout's environment.

## Logs

Each `build`, `br`, `make`, and `cal` writes a log to `.bbot/logs/<UTC time>-<pid>-<command>.log` and prints its path as `Log: <path>`. The log holds the `bbot` invocation, the source, its commit and state, the direnv check, each command with the directory it ran in, the command's output, its exit status, and why a build was refused once it started. Refusals before that, such as an invalid `--source`, appear only on the console. It holds no environment values. Each command line, `$ (in <directory>) <command>`, and its output stream to stderr as well as the log (so `bbot make` output goes to stderr, not stdout), and the exit status of a failed command is `bbot`'s exit status. Child output is not filtered: the log is private to you in `.bbot/`, beside the copies that already contain the credentials. The newest 20 logs are kept; other files in `.bbot/logs/` are left alone.
