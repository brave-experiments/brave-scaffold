# Skills

Skill source lives directly in `.agents/skills/`. `.claude/skills` links to that
directory so both clients use the same files.

- [compare-and-improve](../.agents/skills/compare-and-improve/SKILL.md): Compare two
  implementations or written proposals and strengthen the selected result using
  evidence from either, while preserving review-only requests.
- [bcore-full-build-context](../.agents/skills/bcore-full-build-context/SKILL.md):
  Build Brave for macOS or Android and run tests changed by the selected branch or
  working tree, including guarded Android test preparation.
- [simplify-code](../.agents/skills/simplify-code/SKILL.md): Find duplicated logic and
  remove needless code and structure while preserving behavior and making the
  result easier to understand.
- [testing-preflight](../.agents/skills/testing-preflight/SKILL.md): Choose tests that
  catch meaningful faults without adding redundant checks.
