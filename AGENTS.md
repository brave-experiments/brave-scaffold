# Development guidance

Read README.md first. Implement the requirements supplied for the current task;
do not infer missing requirements from an unrelated repository.

## Naming and attribution

Use names that describe the work. Do not include an agent, model, or tool name as
an authorship marker in branches, worktrees, commits, pull requests, code,
comments, files, or documentation. Use branch names such as `add-checkout-context`
or `fix-command-logging`, without an agent-name prefix.

Do not add generated-by notices, assistant signatures, or AI co-author trailers.
Keep legitimate product names, client integration paths, and required upstream
copyright or license notices when they describe a real dependency or interface.
Use the configured human Git identity; never substitute an agent identity.

## Product boundaries

`bcore` handles Brave Core checkout operations: readiness checks, sync, build,
test, run, and related inspection. It is not the general entry point for Brave
developer tooling. A feature belongs here because it operates on a Core checkout,
not merely because Core developers use it.

Brave Scaffold is entirely supplementary to Brave Core and optional to use.
Adopting or using it must require no changes to Brave Core. Preserve Core's
supported standalone workflow. State this clearly in the README and setup guides;
distinguish integration from the normal writes of explicitly requested browser
operations.

- Keep tooling implementation, launchers, dependencies, and tests under `scripts/`.
  Do not create root-level `src/` or `tests/` directories for tooling.
- Use Python 3.14 or newer. Prefer standard-library code and tests without
  third-party Python dependencies. Create the tooling runtime explicitly at
  `scripts/.venv` with `venv --without-pip`; launchers use its absolute interpreter
  path, never a browser runtime or a silent PATH fallback.
- Keep machine-specific checkout paths and generated state in ignored local
  configuration. Browser checkouts do not need to live in this repository.
- Scaffold setup must not write integration files, hooks, Git configuration, or
  exclusions inside Brave Core. Requested builds and source operations have
  their own declared mutation scope.
- Use direnv for approved external environments. Do not approve environments
  automatically or perform installation, sync, or builds during shell activation.
- Infer the checkout from cwd unless explicitly selected; require a precise
  selection when unclear. Default the platform to the host unless overridden.
- Only one operator may use a checkout at a time initially. Do not add a lock or
  scheduling system without a new requirement.
- Save effective commands, working directories, and streamed child output in a
  redacted diagnostic log. Normal console output shows phases and primary commands;
  `--verbose` also shows probes. Keep diagnostics separate from structured stdout.

## Where to read next

Contributors: [docs/development.md](docs/development.md) covers the layout, tests,
and how to add commands. Agents calling the tools:
[docs/agent-workflows.md](docs/agent-workflows.md) covers authority, human
approval, and reporting. Implementation lives in `scripts/src/scaffold/`; tests in
`scripts/tests/` run with `cd scripts && .venv/bin/python -m unittest discover -s tests -t .`.

Approval boundaries: builds, tests, launches, syncs, tool repair, and cleanup on a
real checkout or device need the user's authorization for that target. A repair
suggestion or passing check grants none. Only the user runs `direnv allow`.

## Agent instructions and skills

`AGENTS.md` is the instruction source. Keep `CLAUDE.md` as a relative symlink to
it. `.agents/skills/` is the project discovery directory; keep `.claude/skills`
as a relative symlink to it. Edit shared instructions once.

Place operational skill source directly in `.agents/skills/`. Do not replace
user-owned entries. These skills configure this repository only; they do not
permit writes inside Core or prove that a client can discover skills from an
external environment.

## Execution and artifact handling

Forward unknown package arguments unchanged. Interpret supported output-affecting
options so readiness, output selection, and records match the effective build.
Forwarded values may replace defaults; conflicts with explicit scaffold selectors
must fail before mutation. Combined build/run commands must identify the actual
build output before restarting the browser; an older default artifact is not a
substitute for unresolved output.

A failed or interrupted rebuild can partly overwrite an earlier output. Mark its
previous success record as needing revalidation before writes begin. Preserve
history and untouched outputs. Valid older artifacts may still launch after
inspection with stale/unknown warnings; do not treat an old receipt as proof that
the current output remains intact. Do not rebuild, delete, or roll back implicitly.

## Changes and verification

Make small, reviewable changes. Test observable behavior and relevant failure
paths. Keep fast tests independent of real browser checkouts, SDKs, credentials,
and user shell configuration. Run expensive browser acceptance checks only on
explicitly selected checkouts. Report what was checked and what remains untested.

Describe the product as it stands. Do not put external planning records,
implementation handoff notes, predecessor paths, or development-history narratives
in this repository. External specifications may guide work without being copied,
linked, or committed here.

## Copyright and license

This project uses the Mozilla Public License 2.0 in `LICENSE`.

Add the following header to new first-party source files, tests, scripts, and
comment-capable configuration or executable templates. Use the file's comment
syntax and its creation year (2026 for files created this year):

```python
# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
```

Keep a required shebang first and any required encoding declaration in its valid
position, then the header before imports or executable code. Preserve existing
copyright years, ownership, and license notices in reused or modified files; do
not replace third-party notices with a Brave header or relicense material merely
by copying it. Required attribution takes precedence over the rule against
research-history references.

Do not add headers to JSON or other formats without comments, generated files,
lockfiles, binary assets, empty marker files, symlinks, or verbatim license texts.
Ordinary Markdown prose need not repeat the source header; the root license
applies, and any existing notices must remain. For formats where a required
notice cannot appear inline, retain it in the appropriate accompanying notice
file. Review header coverage and retained notices before committing new files.

## Git workflow

Inspect status and stage only the current change. All commits must follow
Conventional Commits: `type(optional-scope): description`, for example
`feat: add checkout selection`, `fix(env): preserve the selected checkout`, or
`docs: explain setup`. Use `!` or a `BREAKING CHANGE:` footer for breaking changes.
Describe what changed and why in plain words. The required unsigned marker is
the sole prefix exception: `🚧 docs: explain setup`.

At the end of every turn that changes files, commit that turn's validated change as a
Conventional Commit, staging only its files. Also commit at logical breakpoints within a turn. Do not amend, rebase, or rewrite existing commits without a request.
Do not push unless explicitly requested.

Attempt a signed commit first. If signing fails or times out, confirm HEAD did
not advance and all non-signing checks passed before making one unsigned attempt.
An unsigned subject must start with `🚧`. Report the signing failure and unsigned
status. Never bypass hooks or change persistent signing policy to make a commit
succeed. A marker is not a signature and does not bypass push checks.

## Writing

Use plain, direct English. Keep facts and technical terms precise. Cut words that
add no meaning. Describe behavior and evidence without achievement claims or
agent attribution.
