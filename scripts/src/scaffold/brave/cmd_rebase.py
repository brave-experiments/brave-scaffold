# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Rebase Core without leaving conflicts for the caller to resolve."""

import os
from pathlib import Path

from ..common.procs import run_capture, run_streaming
from ..common.results import Cancelled, Result, ScaffoldError
from .records import track


def run_rebase(ctx):
    identity = ctx.identity()
    core = str(identity.core)
    argv = ['git', '-C', core]
    selectors = {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_PREFIX', 'GIT_NAMESPACE'}
    env = {key: value for key, value in os.environ.items() if key not in selectors}
    env.update(GIT_EDITOR='true', GIT_SEQUENCE_EDITOR='true')

    def read(*args):
        result = run_capture([*argv, *args], core, env, ctx.log)
        if result.returncode or result.timed_out or result.truncated:
            raise ScaffoldError('READINESS_INCOMPLETE', 'Could not inspect Git state; rebase stopped.')
        return result.stdout.strip()

    def active(names):
        return any((Path(core) / read('rev-parse', '--git-path', name)).exists() for name in names)

    if os.path.realpath(read('rev-parse', '--show-toplevel')) != os.path.realpath(core):
        raise ScaffoldError('PREPARATION_CONFLICT',
                            'The selected Core directory is not a Git repository root; rebase stopped.')
    rebase_states = ('rebase-merge', 'rebase-apply')
    if active((*rebase_states, 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'sequencer', 'BISECT_START')):
        raise ScaffoldError('PREPARATION_CONFLICT', 'Finish or abort the existing Git operation before rebasing.')
    branch = read('symbolic-ref', '--quiet', '--short', 'HEAD')
    original = read('rev-parse', 'HEAD')
    if read('status', '--porcelain=v1', '--untracked-files=all'):
        raise ScaffoldError('PREPARATION_CONFLICT', 'Rebase requires a clean checkout, including no untracked files.')

    def run(*args):
        return run_streaming([*argv, *args], core, env, ctx.log, json_mode=ctx.json_mode)

    def restore():
        if active(rebase_states) and run('rebase', '--abort') != 0:
            raise ScaffoldError('PREPARATION_CONFLICT',
                                'Rebase failed and automatic abort failed. Inspect the checkout before continuing.')
        if (read('rev-parse', 'HEAD') != original or read('symbolic-ref', '--quiet', '--short', 'HEAD') != branch
                or read('status', '--porcelain=v1', '--untracked-files=all') or active(rebase_states)):
            raise ScaffoldError('PREPARATION_CONFLICT',
                                'Rebase stopped, but the original branch state could not be verified. Inspect the checkout.')

    with track(ctx, 'rebase', identity, {'base': 'origin/master', 'branch': branch}) as op:
        ctx.log.phase('Fetching origin/master…')
        code = run('fetch', '--no-tags', '--no-recurse-submodules', 'origin',
                   '+refs/heads/master:refs/remotes/origin/master')
        if code:
            raise ScaffoldError('CHILD_FAILED', 'Fetch failed; rebase was not started.', child_exit_code=code)
        base = read('rev-parse', 'refs/remotes/origin/master')
        read('merge-base', base, original)
        if read('rev-list', '--merges', base + '..' + original):
            raise ScaffoldError('PREPARATION_CONFLICT', 'Branch contains merge commits; rebase was not started.')
        ctx.log.phase('Rebasing onto origin/master…')
        try:
            code = run('-c', 'rebase.autoStash=false', '-c', 'rebase.updateRefs=false', '-c', 'rerere.enabled=false',
                       'rebase', '--merge', '--no-autostash', '--no-update-refs', '--no-autosquash',
                       '--no-rebase-merges', '--no-fork-point', base)
        except (Cancelled, OSError):
            restore()
            raise
        if code:
            restore()
            raise ScaffoldError('CHILD_FAILED',
                                'Rebase did not complete cleanly. Restored the original branch state.', child_exit_code=code)
        result = Result(command='rebase', data={'branch': branch, 'base': base, 'original_head': original,
                                               'head': read('rev-parse', 'HEAD')},
                        text='✅ Rebased onto origin/master. Core dependencies were not synced.')
        return op.complete(result)
