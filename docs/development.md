# Development

## Layout

```text
scripts/
  bdev, bpm                 launchers (POSIX sh); resolve their own installation
  pyproject.toml            project metadata; no dependencies
  .venv/                    scaffold runtime (ignored)
  schemas/                  versioned JSON schemas for results
  src/scaffold/
    launch.py               interpreter check, then dispatch
    entry.py                launcher name to command line
    common/                 shared, product-neutral pieces
    brave/                  Brave commands
  tests/{unit,integration,acceptance}/
```

Tooling code, launchers, and tests live under `scripts/`; there is no root
`src/` or `tests/`.

| Module | Responsibility |
| --- | --- |
| `common/results.py` | Result envelope, exit codes, error type, text rendering |
| `common/config.py` | TOML parsing, validation, in-place record edits |
| `common/identity.py` | Checkout discovery, selection, Git linked-worktree detection |
| `common/env.py` | Pure export derivation, direnv approval, explicit loading, identity checks |
| `common/tools.py` | Checkout-local Node/package-manager/Python inspection and argv construction |
| `common/procs.py` | Subprocess execution, command logs, redaction, cancellation |
| `common/cli.py` | Option parsing, forwarding rules, help rendering |
| `common/checks.py` | Check records and readiness aggregation |
| `common/platforms.py` | Targets and the capability table |
| `brave/registry.py` | The command table: parsing, help, and dispatch all read it |
| `brave/execution.py` | The selected checkout's execution context: identity, approved environment, resolved tools |
| `brave/patch_inventory.py`, `brave/sync_scope.py` | Which repositories Core patches and a sync can reset, and what their metadata records |
| `brave/cmd_build.py`, `brave/cmd_patches.py`, `brave/cmd_tools.py`, `brave/cmd_setup.py`, `brave/clean.py`, `brave/android.py`, `brave/doctor.py` | Command handlers and their platform code |
| `brave/sync.py` | Sync arguments, the target list kept across syncs, the local-work guard, and the sync phase |
| `brave/packages.py` | The one place a package command is turned into a child process (bpm and every phase use it) |
| `brave/patches.py`, `brave/android_deps.py`, `brave/freshness.py`, `brave/output_freshness.py` | Patch preparation, Android support preparation, and freshness evidence |
| `brave/support_scripts.py`, `brave/support_script_contracts.json` | Reviewed shell identities and complete support write manifests |
| `brave/records.py`, `brave/steps.py` | Operation records with phase outcomes, per-output history, and the step descriptions shared by plans and records |

Handlers receive a context and return a `Result` or raise `ScaffoldError`; the
runner turns either into exactly one output document.

## Set up and test

```sh
python3.14 -m venv --without-pip scripts/.venv
cd scripts
.venv/bin/python -m unittest discover -s tests -t .
```

Tests use only the standard library. Fast suites need no real checkout, SDK,
credentials, or shell configuration: they build disposable checkouts with fake
Node, package-manager, and `vpython3` executables, use a temporary `HOME` and
direnv data directory, and run the real launchers and a real `direnv`. Fixtures
live in `scripts/tests/support.py`. In a sandbox only, tests approve
environments with `direnv allow`; nothing approves a real environment.

### What the fast suite needs

- **A macOS host.** Restart, launch, and application tests use fake `.app` bundles, `ps`,
  `open`, and `osascript`; the integration classes skip elsewhere.
- **`direnv`** on `PATH`. Tests run a real `direnv` with a private data directory; without
  it they skip.
- **A C compiler (`cc`, from the Xcode command line tools).** Fake applications run a small
  executable the suite compiles once, because copies of system binaries are killed on
  macOS. Without a compiler the fake applications cannot stay running and the restart
  tests fail.
- **Process inspection.** `ps` must be allowed; a sandbox that denies it fails the restart
  tests.
- **Git, and `git-lfs`** for the large-file tests (they skip without it). No network access
  is needed.
- **Python 3.14+** as the scaffold runtime.

The full suite takes about ten to fifteen minutes. Run a focused set while you work, for example
`.venv/bin/python -m unittest tests.integration.test_clean tests.integration.test_plans`, or one
unit module such as `tests.unit.test_buildopts`; unit tests need no direnv, and integration classes
name what they need in their skip messages. The one timing-sensitive test is
`RestartLoggingTests.test_liveness_and_launch_probes_are_logged_with_polling_summarised`: on a heavily
loaded machine it can see fewer liveness polls than it expects; rerun it alone before treating it as a fault. Each test sandbox stops every process that
mentions its directory when it finishes, so a run leaves nothing behind. `test_doc_examples`
runs the command examples from the guides in fixtures; a new example in a guide must be
added to its table (or listed with the reason it cannot run) or a coverage test fails.

Real-checkout acceptance runs are opt-in, use an explicitly selected checkout,
and need the user's authorization for the operations involved
([agent workflows](agent-workflows.md)). Record the checkout, operations, and
results with the evidence.

## Adding a command or check

1. Add a `CommandSpec` to `brave/registry.py` with a summary, positionals,
   options, side effects, and examples. Help and parsing come from it.
2. Write the handler. Take the checkout from `ctx.identity()`, load its environment once with
   `brave/execution.load` (resolve tools with `execution.resolve_tools`), pass that
   `Execution` to every phase, and run processes through `common/procs` (package commands
   through `brave/packages.run`) so they are logged and cancellable. Record each phase with
   `op.start`, `op.succeed`, and `op.fail`.
3. Return a `Result`; raise `ScaffoldError` with a stable code and repairs.
4. Add the command's `data` shape to `schemas/command-data.schema.json` and select it
   in `schemas/result-envelope.schema.json`; a test fails for a command without one.
   Add tests through the real launcher, including the failure paths: the suite
   validates real results (success, unresolved output, error, cancellation, plans)
   against the schemas and checks that help examples parse.
5. Update `docs/commands.md`, the troubleshooting entry for new errors, and the
   capability table.
6. For a doctor check, add it to a check group and the group to a scope in the tables in
   `brave/doctor.py`; execution paths reuse the same functions. Groups other than machine,
   shell, and signing run in the selected checkout's approved environment.

## Sync preservation evidence

`brave/sync_model.py` checks the selected Core implementation and reads its
effective configuration through `sync_probe.mjs`. The probe has read permission
only. `sync_contracts.json` identifies reviewed sync sources, version writers,
and hook inventories with their source digests and conservative output scopes.
An implementation change requires reviewing its mutation behavior before adding
its digest. Never infer a hook's output scope from its name or silently accept a
new script. Include pre-DEPS hooks and recursive DEPS files; `--nohooks` does not
skip pre-DEPS hooks.

The same model supplies plans and execution guards. Patch metadata identifies
materialized bytes even when the patch input has changed. A sync baseline accepts
new output, not unchanged developer work. Regression fixtures must distinguish
the index, working bytes, and untracked collisions, and cover both reset and
non-reset operations.

## Evidence for support claims

A combination is `supported` in the capability table only after it has run on a
real checkout; add it to `VALIDATED` in `common/platforms.py` in the change that
records that evidence. Fixture results never justify `supported`.

## Headers and commits

New first-party source, tests, scripts, and comment-capable configuration carry
the MPL header shown in [AGENTS.md](../AGENTS.md). Commits use Conventional
Commits and follow the signing procedure there.
