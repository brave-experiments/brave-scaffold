---
name: simplify-code
description: Find and simplify duplicated logic, needless code, and excess structure while preserving behavior and making the result easier to understand. Use for focused simplification of a change or selected code. A request to simplify does not authorize feature changes or a repository-wide cleanup.
---

# Simplify code

Aim for code from which little can be removed without losing required behavior
or useful understanding. Every remaining part should do necessary work or make
that work easier to follow. Concision means less to understand and maintain;
shorter source alone is not evidence of improvement.

## Ask what would be lost

Read the requested code and its callers before editing. When no scope is named,
start with the current change. Establish what must stay the same, including
outputs, side effects, failure behavior, and any compatibility, ordering, or
performance constraints that matter here.

For a candidate deletion or replacement, ask: **What would become incorrect,
harder to understand, or harder to change if this were gone?**

- If nothing would be lost, remove it.
- If it performs needed work in a roundabout way, express that work directly.
- If it supplies a useful name, boundary, or guarantee, retain that value even
  when another form would take fewer lines.
- If its purpose is unclear, inspect the relevant callers, contract, or history
  before deciding. Unexplained code is not proof of unnecessary code.

Judge the whole affected path. Removing a helper is no gain if every caller must
now repeat its logic. Moving complexity into configuration, a dependency, or a
dense expression does not remove the need to understand it.

## Remove what adds no value

Look for decisions made twice, stored values that needlessly duplicate other
state, conversions that cancel out, and branches whose outcomes are equivalent.
Check evaluation order and side effects before combining them. Recomputing a
stored value may change its meaning or cost.

Question layers that only pass arguments through, options with no supported use,
and general frameworks built for a single concrete need. Replace them with the
smallest design that serves the actual contract. One caller alone is not a reason
to inline a function: its name or boundary may explain an important operation.

Actively look for duplicated logic in the selected code and related helpers or
callers. Search for the same rule or sequence expressed with different names as
well as copied blocks. Identify cases where a rule change would require matching
edits in several places. Prefer reusing an existing operation or extracting a
small shared function when it reduces that repeated work and remains easy to use.

Compare inputs, side effects, and failure behavior before combining duplicates.
Similar-looking code may implement different rules; merging it can introduce
modes and conditions that cost more than the repetition. Keep those distinctions.
An abstraction helps when it lets callers reason about less detail. Report useful
duplication findings outside the requested scope without expanding the cleanup.

Remove comments that merely repeat an operation. Keep reasons, constraints, and
warnings that the code cannot express. Prefer clear names and intermediate values
to compressed expressions that require the reader to work out hidden steps.

Treat guards, retries, cleanup, and compatibility paths as behavior. Remove one
only with evidence that its case cannot occur under the supported contract or
that another path provides the same protection. A happy-path test is not enough.

Do not weaken assertions to make a refactor pass. Remove or revise tests only
when their contract is obsolete, covered elsewhere, or tied to an internal detail
that the change legitimately removes. Preserve tests of externally required
behavior even when they make simplification less convenient.

## Finish when further cuts would hurt

Make focused edits, then read the result in execution order and from its callers.
Keep a change only if it reduces needless work or makes the reasoning clearer.
Leave unrelated code alone. If a possible improvement requires a behavior change,
report it separately instead of including it in the cleanup.

Run checks that exercise the affected contract and required repository checks.
Use existing coverage where it is enough; add a test only for a meaningful gap.
Report what became simpler, the evidence that behavior remains intact, and any
unverified risk. A result with no justified edits is valid. Stop when further
cuts would trade away behavior, useful explanation, or a sound boundary.
