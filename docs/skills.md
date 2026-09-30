# Skills

Skill source lives directly in `.agents/skills/`. `.claude/skills` links to that
directory so both clients use the same files.

- [simplify-code](../.agents/skills/simplify-code/SKILL.md): Find duplicated logic and
  remove needless code and structure while preserving behavior and making the
  result easier to understand.
- [testing-preflight](../.agents/skills/testing-preflight/SKILL.md): Choose tests that
  catch meaningful faults without adding redundant checks.
