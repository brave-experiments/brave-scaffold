# Agent workflows

How agents and scripts should call the tools. The CLI enforces its command
contracts and technical checks. It does not read conversation, infer permission
from flags, or approve anything: authority comes from the user.

## Authority and human intervention

Once a user authorizes a task and its scope, continue without repeated
confirmation. Ask only when a required choice is unresolved, an action exceeds
the scope, or human approval is expressly required.

| Situation | Action |
| --- | --- |
| Reading source, running the tools' read-only commands, isolated tests | Proceed |
| Generating environment or configuration files outside Core | Prepare and show them; the user reviews and runs `direnv allow` |
| Builds and tests on a checkout the user named | Proceed when the checkout and operations are authorized and guards pass |
| Device install/run, source sync, tool repair, output deletion | Proceed only if the request or agreed plan covers the action and target; otherwise prepare the exact plan and ask |
| Checkout or device ambiguity, conflicting local edits, another operator | Stop that operation, investigate without overwriting work, ask for the missing choice; continue independent work |
| Credentials, 1Password, machine-level setup | Use existing authorized access; the user supplies human approval |
| Deferred features, changed scope, weaker safeguards | Needs a separate user decision |
| Pushing or publishing | Needs explicit authorization; permission to commit locally does not include it |

Sync runs Core's package script with its normal source and dependency effects.
Authorization to sync covers that operation, including the effects of forwarded
options. Scaffold adds no local-work preservation checks or overwrite prompt to
sync. Inspect and save wanted work before requesting source changes. Read-only
plans need no sync authority and do not establish which local files Core will
preserve.

A suggested repair in a result, or a passing `doctor`, grants nothing. `requires_user_action` says only whether a step needs a person; `false`
does not mean it is permitted. For an unresolved checkout, list candidates with
`bdev checkout list`; never pick one yourself.

An implementation task authorizes code changes and isolated tests. It does not by
itself authorize builds, tests, or launches on a developer's checkout or device.

Support-repository sync, explicit discard, and prune deletion also need authority
for the scaffold's `support/` directory. Use `scripts/sync-support-repos --status`
or `--prune` for inspection; neither authorizes updates or removal.

## Example validation agreement

State once, before real-checkout work:

- the checkout (an alias or path) and, for mobile, the device;
- the operations allowed, for example "inspect, run `bpm` package commands, run
  `bdev tools setup` if a payload is stale";
- that the agent has exclusive use for the period.

Do not infer exclusive use from the absence of a visible build process. If
concurrent use appears, stop the affected work. Only one operator may use a
checkout at a time; the scaffold does not lock.

## Calling the tools

- Every call is a fresh process. Nothing depends on a shell hook, activation, or
  state from an earlier call; pass `--checkout` explicitly unless the working
  directory is inside the checkout.
- For `bdev` and `bpm`, use `--json`. Read `status`, `error.code`, and `exit_code`; treat
  `child_exit_code` as the child's own result. Stdout has one document; child
  output is on stderr.
- Interpret `error.repairs` as suggestions. Run one only when the current task
  authorizes its effect. Approval repairs (`direnv allow`) are for the user.
- `bpm` and `bdev vpython3` forward everything after their leading scaffold
  options unchanged; use `--` to forward a token that collides with a scaffold
  option.
- Use `bdev doctor <scope> --checkout <name>` to learn readiness before a
  workflow. It changes nothing.
- On cancellation, inspect the checkout before retrying anything that modifies it.

## Reporting

State what was run against which checkout, exit codes, and what was not run.
Distinguish fixture tests from real-checkout evidence.

## Commits

Attempt a signed commit first. If signing fails or times out, confirm `HEAD` did
not advance and that all non-signing checks passed, then make one unsigned
attempt whose subject starts with `🚧`. Report the signing failure and that the
commit is unsigned. Never bypass hooks or change persistent signing policy; the
marker is not a signature. See [AGENTS.md](../AGENTS.md).
