---
name: testing-preflight
description: Choose tests for a code change and assess whether new or changed assertions catch meaningful faults. Use before adding tests, planning validation, or reviewing test value. Keep checks tied to the affected behavior; this is not a request for a repository-wide test audit.
---

# Testing preflight

Choose tests for the confidence they add. Test count, lines covered, and the size
of a test suite are not goals. A useful test catches a plausible mistake at a
reasonable cost to write, run, and maintain.

## Decide what needs evidence

Read the task, relevant code, nearby tests, and required repository checks.
Before writing a test, establish:

- What observable behavior or contract must hold?
- What plausible mistake would violate it?
- What input and assertion distinguish that mistake from correct behavior?
- What does this add beyond existing tests or other required checks?

Keep this reasoning brief. Do not create a separate plan or checklist file unless
the task needs one. Derive expected results from the requirement or an independent
contract, not by copying the implementation's calculation.

Reuse or extend existing coverage when it already reaches the relevant behavior.
Choose the narrowest test that can expose the fault. Test an integration boundary
when wiring, state transfer, or interaction between components is the risk.
Repeating a scenario at another layer needs a distinct reason.

No new test is a valid outcome when existing checks cover the risk or a direct
inspection is enough for a small, low-risk change. Explain that choice briefly.
Documentation and cosmetic edits do not automatically need runtime tests; use
the relevant rendering, link, build, or other checks where needed. Preserve
explicit user requirements and mandatory repository checks.

## Make the assertion earn its place

Exercise the real decision or effect under test. Mocks can control dependencies,
but must not supply the behavior the test claims to verify. Avoid assertions that
only check that a mock returned its configured answer or a function was called
when the requirement concerns the resulting state.

Use fixtures that make likely mistakes visible. If choosing the wrong record is
the risk, give records different relevant values and assert which record changed.
Two identical records and an assertion that something changed cannot prove the
selection was correct.

Be skeptical of copied implementation logic, snapshots nobody can assess, source
text matching, and private call details. Exact bytes, call order, or source rules
can still deserve tests when they are the actual contract. Judge what failure an
assertion detects, not its syntax. Avoid changing production APIs solely to let
tests inspect internals when a real entry point can prove the behavior.

Cover distinct affected states and paths where they can fail differently. Do not
generate every input combination without a reason. Shared helper coverage does
not prove that each affected caller passes the right data or handles its result.

When relaxing or removing an assertion, identify what protection would be lost
and whether it remains elsewhere or is obsolete. Revisit unchanged assertions
when output becomes less specific: distinct fixtures may now look identical.
Do not turn a focused change into unrelated test cleanup.

For asynchronous behavior, use an observed event or explicit synchronization to
reach the state being tested. Use bounded waits; a fixed sleep alone does not
prove that the relevant work started.

## Check that the evidence supports the claim

For a bug fix, demonstrate that the focused regression test fails on the faulty
behavior and passes with the fix when practical. For important or uncertain
assertions, a small temporary fault can check whether the test detects the claimed
mistake. Use this where it adds confidence, not as a ritual for every test.

Use an isolated copy or a reversible edit that preserves all existing work.
Confirm that the failure comes from the intended assertion, not broken setup,
a compile error, or an unrelated timeout. Restore the exact saved contents,
inspect the diff for leftover faults, and rerun the focused check. If this is
unsafe or impractical, state that protection was reasoned about, not demonstrated.
Do not add a mutation framework just for this exercise.

Run focused checks and the required repository checks. Broaden testing when
shared behavior, affected consumers, failures, or unresolved risk justify it.
After they pass, stop unless later changes invalidate the evidence. In the final
report, state what was checked, the results, and material gaps. Distinguish a
passing test from demonstrated detection of a fault; a pass count proves neither
that assertions are useful nor that untested paths work.
