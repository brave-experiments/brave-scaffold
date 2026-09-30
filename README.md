# Brave development scaffold

Local tools for building, testing, running, and inspecting Brave checkouts across
platforms. Browser checkouts and support repositories may live anywhere and are
selected through local configuration.

This repository currently contains project guidance and agent discovery setup.
Build and setup commands are not implemented yet.

Tooling source, launchers, and tests belong under `scripts/`. Shared documentation
belongs under `docs/`, and operational skill source belongs under `skills/`.

Read [AGENTS.md](AGENTS.md) before contributing. `CLAUDE.md` links to the same
instructions. Project skill discovery uses `.agents/skills/`; `.claude/skills`
links to that directory. The `testing-preflight` skill helps choose tests that
catch meaningful faults without adding redundant checks.
The `simplify-code` skill removes needless code and structure while preserving
behavior and making the result easier to understand.
