# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Sync the root support manifest into the scaffold's support directory."""

import argparse
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

from .common.identity import discover_core
from .common.procs import CommandLog, install_signal_handlers, run_capture, run_streaming
from .common.redaction import redact_url_credentials
from .common.results import Cancelled, ScaffoldError


class Git:
    def __init__(self, log):
        self.log = log
        # A caller's Git selectors must not redirect operations into its checkout.
        selectors = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE"}
        self.env = {key: value for key, value in os.environ.items() if key not in selectors}
        self.env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")

    def read(self, directory, *arguments):
        result = run_capture(["git", *arguments], cwd=str(directory), env=self.env, log=self.log)
        if result.returncode or result.truncated or result.timed_out or result.cleanup_incomplete:
            raise ScaffoldError("PREPARATION_CONFLICT", "Cannot inspect %s with git %s." % (directory, arguments[0]))
        return result.stdout.strip()

    def run(self, directory, *arguments):
        code = run_streaming(["git", *arguments], cwd=str(directory), env=self.env, log=self.log)
        if code:
            raise ScaffoldError("CHILD_FAILED", "Git failed in %s; inspect it before retrying." % directory)


def load_manifest(path):
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"repos"} or not isinstance(data["repos"], list):
        raise ScaffoldError("INVALID_INPUT", "The manifest must contain a repos array.")
    names = set()
    for repo in data["repos"]:
        if not isinstance(repo, dict) or set(repo) != {"name", "url", "branch"}:
            raise ScaffoldError("INVALID_INPUT", "Each repository needs name, url, and branch.")
        if any(not isinstance(value, str) or not value or any(ord(char) < 32 for char in value)
               for value in repo.values()):
            raise ScaffoldError("INVALID_INPUT", "Repository fields must be nonempty text without control characters.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", repo["name"]) or repo["name"] in names:
            raise ScaffoldError("INVALID_INPUT", "Repository names must be unique single directory names.")
        if repo["url"].startswith("-") or repo["branch"].startswith("-"):
            raise ScaffoldError("INVALID_INPUT", "URLs and branches cannot start with '-'.")
        names.add(repo["name"])
    return data["repos"]


def inspect(git, directory):
    if directory.is_symlink() or not (directory / ".git").is_dir():
        raise ScaffoldError("PREPARATION_CONFLICT", "Path is not a standalone Git checkout.")
    if discover_core(directory):
        raise ScaffoldError("PREPARATION_CONFLICT", "Path overlaps a browser checkout.")
    if Path(git.read(directory, "rev-parse", "--show-toplevel")).resolve() != directory:
        raise ScaffoldError("PREPARATION_CONFLICT", "Git root differs from the repository path.")
    return (git.read(directory, "branch", "--show-current"), git.read(directory, "rev-parse", "HEAD"),
            bool(git.read(directory, "status", "--porcelain=v1", "--untracked-files=all")))


def sync(git, repo, directory, discard):
    if not directory.exists() and not directory.is_symlink():
        directory.parent.mkdir(parents=True, exist_ok=True)
        git.run(directory.parent, "clone", "--branch", repo["branch"], "--", repo["url"], str(directory))
        return "cloned"
    branch, _, dirty = inspect(git, directory)
    if branch != repo["branch"]:
        raise ScaffoldError("PREPARATION_CONFLICT", "Current branch differs from the manifest.")
    if git.read(directory, "remote", "get-url", "origin") != repo["url"]:
        raise ScaffoldError("PREPARATION_CONFLICT", "Origin URL differs from the manifest.")
    if dirty and not discard:
        raise ScaffoldError("PREPARATION_CONFLICT", "Working tree has local files or changes.")
    remote = "refs/remotes/origin/" + branch
    git.run(directory, "fetch", "origin", "+refs/heads/%s:%s" % (branch, remote))
    if discard:
        git.run(directory, "reset", "--hard", remote)
        return "reset"
    try:
        git.read(directory, "merge-base", "--is-ancestor", "HEAD", remote)
    except ScaffoldError:
        raise ScaffoldError("PREPARATION_CONFLICT", "Local history cannot fast-forward to the fetched branch.")
    git.run(directory, "merge", "--ff-only", "--no-overwrite-ignore", remote)
    return "updated"


def prune_reason(git, directory):
    branch, _, dirty = inspect(git, directory)
    if dirty or git.read(directory, "status", "--porcelain=v1", "--ignored", "--untracked-files=all"):
        return "Working tree has changed, untracked, or ignored files."
    if not branch:
        return "HEAD is detached."
    if git.read(directory, "rev-parse", "--is-shallow-repository") == "true":
        return "Shallow history cannot prove commits are saved."
    if git.read(directory, "stash", "list"):
        return "Repository has stashes."
    if git.read(directory, "worktree", "list", "--porcelain").count("worktree ") != 1:
        return "Repository has linked worktrees."
    if any(line.startswith("160000 ") for line in git.read(directory, "ls-files", "--stage").splitlines()):
        return "Repository contains submodules."
    refs = git.read(directory, "for-each-ref", "--format=%(refname)").splitlines()
    local = [ref for ref in refs if not ref.startswith("refs/remotes/")]
    remote = [ref for ref in refs if ref.startswith("refs/remotes/")]
    if not remote or git.read(directory, "rev-list", "--max-count=1", "HEAD", *local, "--not", *remote):
        return "Cannot prove all local commits are saved on remote references."

    def unreadable(error):
        raise error

    for parent, directories, files in os.walk(directory, onerror=unreadable):
        if Path(parent) == directory:
            directories[:] = [name for name in directories if name != ".git"]
        elif ".git" in directories or ".git" in files:
            return "Repository contains a nested Git checkout."
    return None


def main(argv):
    parser = argparse.ArgumentParser(prog="sync-support-repos", allow_abbrev=False,
                                     description="Sync support-repos.toml into this scaffold's support/ folder.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--status", action="store_true", help="inspect without fetching")
    modes.add_argument("--prune", action="store_true", help="preview repositories absent from the manifest")
    modes.add_argument("--discard-local", action="store_true", help="reset tracked files and local commits; requires --execute")
    parser.add_argument("--execute", action="store_true", help="perform prune or discard-local changes")
    parser.add_argument("--repo", help="limit sync or status to one repository")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true", help="hide progress and child output")
    verbosity.add_argument("--verbose", action="store_true", help="also show Git probes")
    args = parser.parse_args(argv)
    if args.discard_local and not args.execute:
        parser.error("--discard-local requires --execute")
    if args.execute and not (args.prune or args.discard_local):
        parser.error("--execute requires --prune or --discard-local")
    if args.prune and args.repo:
        parser.error("--repo cannot select a prune candidate")

    root = Path(__file__).resolve().parents[3]
    support = root / "support"
    log = CommandLog(stream=sys.stderr, verbosity="quiet" if args.quiet else "verbose" if args.verbose else "normal")
    git = Git(log)
    install_signal_handlers()
    try:
        if support.is_symlink() or discover_core(root):
            raise ScaffoldError("OWNERSHIP_CONFLICT", "Support repositories must stay in this scaffold, outside browser checkouts.")
        log.open(root)
        repos = load_manifest(root / "support-repos.toml")
        for repo in repos:
            try:
                git.read(root, "check-ref-format", "--branch", repo["branch"])
            except ScaffoldError:
                raise ScaffoldError("INVALID_INPUT", "Invalid branch for %s." % repo["name"])
        if args.repo:
            repos = [repo for repo in repos if repo["name"] == args.repo]
            if not repos:
                raise ScaffoldError("INVALID_INPUT", "No repository named %r is in the manifest." % args.repo)
        if args.prune:
            kept = {repo["name"] for repo in repos}
            repos = [{"name": path.name} for path in sorted(support.iterdir())
                     if path.name not in kept and (path / ".git").exists()] if support.is_dir() else []
        exit_code = 0
        for repo in repos:
            name = repo["name"]
            directory = support / name
            try:
                if args.prune:
                    reason = prune_reason(git, directory)
                    if reason:
                        raise ScaffoldError("PREPARATION_CONFLICT", reason)
                    if args.execute:
                        log.phase("Removing %s" % directory)
                        shutil.rmtree(directory)
                    message = "removed" if args.execute else "would prune"
                elif args.status:
                    revision, message = "-", "missing"
                    if directory.exists() or directory.is_symlink():
                        branch, revision, dirty = inspect(git, directory)
                        message = "%s %s %s" % (branch or "detached", revision[:12], "dirty" if dirty else "clean")
                else:
                    log.phase("Syncing %s" % name)
                    message = sync(git, repo, directory, args.discard_local)
                print("%s: %s" % (name, message))
            except ScaffoldError as error:
                if error.code != "PREPARATION_CONFLICT":
                    raise
                print("%s: skipped (%s)" % (name, redact_url_credentials(error.message)))
                if not args.status:
                    exit_code = 6
        return exit_code
    except ScaffoldError as error:
        log.message(redact_url_credentials(error.message))
        return error.exit_code
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        log.message(redact_url_credentials(str(error)))
        return 1
    except Cancelled as cancelled:
        log.message("Interrupted; inspect the affected repository before retrying.")
        return cancelled.exit_code
    finally:
        log.close()
        if log.path:
            log.message("Log: %s" % log.path)
