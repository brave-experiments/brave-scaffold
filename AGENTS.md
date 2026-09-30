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

Brave Scaffold is entirely supplementary to Brave Core and optional to use.
Adopting or using it must require no changes to Brave Core. Preserve Core's
supported standalone workflow. State this clearly in the README and setup guides;
distinguish integration from the normal writes of explicitly requested browser
operations.

- Keep tooling implementation, launchers, dependencies, and tests under `scripts/`.
  Do not create root-level `src/` or `tests/` directories for tooling.
- Use Python 3.14 or newer. Prefer standard-library code and tests without
  third-party Python dependencies.
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
- Log full effective commands and their working directories by default, with
  known secrets redacted. Keep command logs separate from structured stdout.

## Agent instructions and skills

`AGENTS.md` is the instruction source. Keep `CLAUDE.md` as a relative symlink to
it. `.agents/skills/` is the project discovery directory; keep `.claude/skills`
as a relative symlink to it. Edit shared instructions once.

Place operational skill source under `skills/` and expose selected skills through
relative links in `.agents/skills/`. Do not replace user-owned entries. These
links configure this repository only; they do not permit writes inside Core or
prove that a client can discover skills from an external environment.

## Changes and verification

Make small, reviewable changes. Test observable behavior and relevant failure
paths. Keep fast tests independent of real browser checkouts, SDKs, credentials,
and user shell configuration. Run expensive browser acceptance checks only on
explicitly selected checkouts. Report what was checked and what remains untested.

Describe the product as it stands. Do not put external planning records,
implementation handoff notes, predecessor paths, or development-history narratives
in this repository. External specifications may guide work without being copied,
linked, or committed here.

## Git workflow

Inspect status and stage only the current change. All commits must follow
Conventional Commits: `type(optional-scope): description`, for example
`feat: add checkout selection`, `fix(env): preserve the selected checkout`, or
`docs: explain setup`. Use `!` or a `BREAKING CHANGE:` footer for breaking changes.
Describe what changed and why in plain words. The required unsigned marker is
the sole prefix exception: `🚧 docs: explain setup`.

Commit at logical breakpoints after validation. Do not amend, rebase, or rewrite existing commits without a request.
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
