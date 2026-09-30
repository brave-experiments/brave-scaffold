# Commit signing

Commits in this repository are signed with an SSH key held by 1Password. This
page covers the read-only checks, the manual setup, how the signer behaves, and
the commit procedure when signing fails. Signing setup lives in your user-level
Git configuration; the scaffold never copies Git settings into Brave Core and
never edits any Git configuration.

## Read-only check

```sh
scripts/bdev doctor signing
```

Required checks: `gpg.format` is `ssh`, `gpg.ssh.program` names an executable
signer, `user.signingkey` is set, and 1Password's `op-ssh-sign` is installed.
Optional checks (warnings only): `commit.gpgsign = true`, and an SSH agent socket
exists. The checks read the effective Git configuration for the scaffold
repository, print no key material, and write nothing. A missing required
check gives `READINESS_BLOCKED` (exit 3). Reachability of 1Password itself is not
tested.

## Manual configuration (you)

Configure Git yourself, at user level or in this repository, following your
organization's signing guide. The relevant settings:

```sh
git config --global gpg.format ssh
git config --global gpg.ssh.program /path/to/scaffold/scripts/git-sign-with-1password
git config --global user.signingkey "key::ssh-ed25519 AAAA..."
git config --global commit.gpgsign true
```

Use your own key. Do this for the scaffold repository only as needed; nothing in
the scaffold applies it to Brave Core checkouts.

## The signer

`scripts/git-sign-with-1password` is a protocol helper that Git calls in place of
`ssh-keygen -Y sign`. It passes Git's arguments and standard input to
1Password's `op-ssh-sign` unchanged and writes only the signer's output to
stdout; it never prints a result document. Diagnostics go to stderr.

- **Time limit:** the whole attempt, including any recovery, has 15 seconds. On
  expiry the signer process group is stopped and the helper exits `124`.
- **Recovery:** if the 1Password agent socket is unavailable, the helper may open
  the 1Password app and retry exactly once, only on a local, unlocked console
  session (never over SSH). It waits about two seconds, rechecks the session, and
  retries with the same arguments. A denial or any other failure is not retried.
- **Cancellation:** SIGTERM or SIGHUP stops the signer's process group and exits
  `130`.
- **Failure:** a failed signing attempt exits nonzero. It is never turned into an
  unsigned result, so Git aborts the commit.

If signing hangs, answer the 1Password prompt on your machine; the helper cannot
approve it for you.

## Commit procedure

1. Attempt a normal signed commit.
2. If signing fails or times out, confirm `HEAD` did not advance
   (`git rev-parse HEAD` unchanged) and that every non-signing check
   (tests, hooks, formatting) passed.
3. Only then make one unsigned attempt:
   `git -c commit.gpgsign=false commit -m "🚧 <type>: <description>"`.
   The subject must start with `🚧`.
4. Never bypass hooks, and never change signing configuration to make a commit
   pass. The marker is not a signature.

A failure that is not a signing failure (a hook, a test, an empty message) does
not permit the unsigned fallback; fix it and retry the signed commit.

## Verify and report

Check a commit with `git verify-commit HEAD` (this needs an
`gpg.ssh.allowedSignersFile`). When reporting work, say whether each commit is
signed. For a fallback, state the signing failure and that the commit is unsigned;
unsigned commits still need signing or explicit acceptance before publication.
