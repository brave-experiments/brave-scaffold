# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Approve, back up, and remove listed local file changes for one sync."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from ..common.redaction import redact_report
from ..common.results import ScaffoldError, detail_lines
from . import gitstate, sync_scope


def unique_conflicts(conflicts):
    grouped = {}
    for item in conflicts:
        reasons = grouped.setdefault(item['path'], [])
        if item['reason'] not in reasons:
            reasons.append(item['reason'])
    return [{'path': path, 'reason': '; '.join(reasons)} for path, reasons in sorted(grouped.items())]


def checked_git(repo, arguments, log):
    result = gitstate._git(repo, arguments, log)
    if result.returncode or result.truncated:
        raise ScaffoldError('PREPARATION_CONFLICT', 'Could not completely inspect or restore sync overwrite files.',
                            details={'repository': str(repo), 'arguments': arguments})
    return result.stdout


def selection(identity, model, conflicts, log):
    scope = sync_scope.sync_repositories(identity)
    if model.blocked or model.unknown or not scope.complete:
        raise ScaffoldError('PREPARATION_CONFLICT', 'Overwrite approval cannot bypass an unknown sync write scope.',
                            details={'files': conflicts, 'scope': model.detail()})
    repositories = sorted(set(scope.repositories) | set(model.repositories), key=lambda p: len(p.parts), reverse=True)
    selected = {}
    for item in unique_conflicts(conflicts):
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
            raise ScaffoldError('PREPARATION_CONFLICT', 'Overwrite approval covers files only, not local commits or unknown paths.',
                                details={'files': conflicts})
        path = identity.src / relative
        repo = next((repo for repo in repositories if path.is_relative_to(repo)), None)
        if repo is None or path == repo:
            raise ScaffoldError('PREPARATION_CONFLICT', 'The affected file has no known repository.', details={'files': conflicts})
        # Do not follow links outside the selected checkout, including linked parent directories.
        if path.is_symlink() or not path.parent.resolve().is_relative_to(repo.resolve()) \
                or (path.exists() and not path.is_file()):
            raise ScaffoldError('PREPARATION_CONFLICT', 'Overwrite approval requires ordinary files inside their repository.',
                                details={'files': conflicts})
        name = str(path.relative_to(repo))
        if name not in gitstate.inspect_changes(repo, log, [name]).all:
            raise ScaffoldError('PREPARATION_CONFLICT', 'This conflict is not an inspectable local file change.',
                                details={'files': conflicts})
        selected.setdefault(repo, []).append(name)
    return selected


def file_evidence(path, repo):
    if path.is_symlink() or not path.parent.resolve().is_relative_to(repo.resolve()):
        raise ScaffoldError('PREPARATION_CONFLICT', 'An overwrite path became a symlink or left its repository.')
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ScaffoldError('PREPARATION_CONFLICT', 'An overwrite path is no longer an ordinary file.')
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        return {'sha256': digest, 'mode': stat.S_IMODE(info.st_mode)}
    except FileNotFoundError:
        return {'sha256': None, 'mode': None}


def fingerprint(selected, log):
    result = {}
    for repo, names in selected.items():
        index = checked_git(repo, ['ls-files', '--stage', '-z', '--', *names], log)
        if any(entry.split('\t', 1)[0].split()[-1] != '0' for entry in index.split('\0') if entry):
            raise ScaffoldError('PREPARATION_CONFLICT', 'Resolve merge conflicts before approving sync overwrites.')
        result[str(repo)] = {
            'head': checked_git(repo, ['rev-parse', 'HEAD'], log), 'index': index,
            'files': {name: file_evidence(repo / name, repo) for name in names},
        }
    return result


def diff(selected, log):
    chunks = []
    for repo, names in selected.items():
        chunks.append('Repository: ' + str(repo))
        for args in (['diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD', '--', *names],
                     ['diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', '--', *names]):
            chunks.append(checked_git(repo, args, log))
        untracked = gitstate.inspect_changes(repo, log, names).untracked
        for name in sorted(untracked):
            shown = gitstate._git(repo, ['diff', '--no-index', '--binary', '--no-ext-diff', '--no-textconv',
                                        '--', os.devnull, str(repo / name)], log)
            if shown.returncode not in (0, 1) or shown.truncated:
                raise ScaffoldError('PREPARATION_CONFLICT', 'Could not show the complete untracked file diff.')
            chunks.append(shown.stdout)
    return '\n'.join(chunks)


def confirm(ctx, conflicts, selected):
    print('Sync may overwrite these files (relative to ' + str(ctx.identity().src) + '):', file=sys.stderr)
    for line in detail_lines({'files': redact_report(conflicts)}):
        print(line, file=sys.stderr)
    if ctx.parsed.get('overwrite_local_changes'):
        return True
    if ctx.parsed.json_mode or not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    while True:
        print('Overwrite these changes? [y/N] (d: show diffs): ', end='', file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip().lower()
        if answer == 'd':
            print(redact_report(diff(selected, ctx.log)), file=sys.stderr)
        elif answer in ('y', 'yes'):
            return True
        else:
            return False


def approve(ctx, identity, model, conflicts, op, recheck):
    if not ctx.parsed.get('overwrite_local_changes') and (ctx.parsed.json_mode or not sys.stdin.isatty() or not sys.stderr.isatty()):
        return None
    selected = selection(identity, model, conflicts, ctx.log)
    before = fingerprint(selected, ctx.log)
    if not confirm(ctx, conflicts, selected):
        return None
    # Repeat the complete guard after the human's review, not just the file hashes.
    current = recheck()
    if unique_conflicts(current) != conflicts or selection(identity, model, current, ctx.log) != selected \
            or fingerprint(selected, ctx.log) != before:
        raise ScaffoldError('PREPARATION_CONFLICT', 'Files changed during review; nothing was overwritten. Repeat sync to review them again.')
    backup = op.root / 'backups' / op.id / 'sync-overwrite'
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    manifest = {'checkout': str(identity.core), 'files': conflicts, 'repositories': {
        str(repo): {**before[str(repo)], 'backup_directory': str(number)}
        for number, repo in enumerate(selected)}}
    for number, (repo, names) in enumerate(selected.items()):
        destination = backup / str(number)
        destination.mkdir(mode=0o700)
        (destination / 'changes.patch').write_text(checked_git(repo,
            ['diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD', '--', *names], ctx.log))
        (destination / 'staged.patch').write_text(checked_git(repo,
            ['diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', '--', *names], ctx.log))
        for name in names:
            path = repo / name
            if path.exists():
                target = destination / 'worktree' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if selection(identity, model, conflicts, ctx.log) != selected or fingerprint(selected, ctx.log) != before:
        raise ScaffoldError('PREPARATION_CONFLICT', 'Files changed while backing them up; nothing was overwritten.',
                            details={'backup': str(backup)})
    op.start('sync-overwrite', files=conflicts, backup=str(backup))
    print('Sync overwrite backup: ' + str(backup), file=sys.stderr)
    for repo, names in selected.items():
        tracked = set(checked_git(repo, ['ls-files', '-z', '--', *names], ctx.log).split('\0'))
        head = set(checked_git(repo, ['ls-tree', '-rz', '--name-only', 'HEAD', '--', *names], ctx.log).split('\0'))
        restore = [name for name in names if name in tracked or name in head]
        if restore:
            checked_git(repo, ['restore', '--source=HEAD', '--staged', '--worktree', '--', *restore], ctx.log)
        for name in names:
            if name not in tracked and name not in head:
                (repo / name).unlink()
    op.succeed('sync-overwrite', backup=str(backup))
    return str(backup)
