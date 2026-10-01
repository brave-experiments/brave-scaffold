# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Read-only evidence of Core's effective sync and hook write scopes."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from ..common.procs import run_capture
from . import gitstate, sync_scope
from .patch_inventory import sha256_or_none

CONTRACTS = Path(__file__).with_name('sync_contracts.json')
PROBE = Path(__file__).with_name('sync_probe.mjs')


@dataclass
class SyncModel:
    resets: set
    writes: dict = field(default_factory=dict)
    unknown: list = field(default_factory=list)
    chromium: str = 'unresolved'
    blocked: bool = False
    generated: set = field(default_factory=set)
    repositories: set = field(default_factory=set)

    def detail(self):
        return {'chromium_sync': self.chromium, 'reset_repositories': sorted(map(str, self.resets)),
                'other_writes': sorted(map(str, self.writes)), 'incomplete_evidence': self.unknown}


def literal_config(path):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        tree = ast.parse(path.read_text())
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = ast.literal_eval(node.value)
                else:
                    raise ValueError("non-literal gclient assignment")
        else:
            raise ValueError("non-literal gclient statement")
    return result


def chromium_will_sync(identity, config, log=None):
    # An explicit false wins even over --force/--init and a changed configuration.
    if config['chromium_option'] is not None:
        return bool(config['chromium_option'])
    if config['force'] or config['gclient_changed']:
        return True
    try:
        receipt = json.loads((identity.workspace / '.brave_latest_successful_sync.json').read_text())
        timestamp = config.get('gclient_timestamp')
        if timestamp is None:
            return True
        # Use Node's own timestamp representation, as Core does.
        if set(receipt) != {'chromiumRef', 'gclientTimestamp'} or receipt['chromiumRef'] != config['chromium_ref'] \
                or receipt['gclientTimestamp'] != timestamp:
            return True
        target = gitstate._git(identity.src, ['rev-parse', '--verify', config['chromium_ref'] + '^{commit}'], log)
        head = gitstate.head_commit(identity.src, log)
        return not head or target.returncode != 0 or target.truncated or target.stdout.strip() != head
    except (OSError, ValueError, TypeError):
        return True


def sync_entrypoint(scripts):
    """Recognize the supported source command and its package installation prefix."""
    if any(scripts.get(name) for name in ('presync', 'postsync', 'preinstall', 'install', 'postinstall', 'prepare')):
        return None
    command = 'node ./build/commands/scripts/sync.ts'
    preinstall = scripts.get('pnpm:devPreinstall')
    if scripts.get('sync') == command and not preinstall:
        return 'node'
    if scripts.get('sync') == 'pnpm install --frozen-lockfile --yes && ' + command \
            and preinstall in (None, 'node ./build/commands/scripts/devPreinstall.ts'):
        return 'pnpm'
    return None


def inspect(identity, toolchain, arguments, environ, log=None):
    scope = sync_scope.sync_repositories(identity)
    model = SyncModel(set(scope.repositories))
    default_depot = identity.core / "vendor/depot_tools"
    if (default_depot / ".git").exists():
        model.repositories.add(default_depot)
        model.writes[default_depot] = "depot_tools installation or self-update"
    contracts = json.loads(CONTRACTS.read_text())
    source_problems = [name for name, digests in contracts['sync_sources'].items()
                       if sha256_or_none(identity.core / name) not in digests]
    if source_problems or toolchain is None:
        model.unknown = ['sync implementation is unverified: ' + ', '.join(source_problems or ['local Node unavailable'])]
        return model
    try:
        package = json.loads((identity.core / "package.json").read_text())
        scripts = package.get("scripts", {})
        entrypoint = sync_entrypoint(scripts)
        if entrypoint is None:
            model.unknown = ["unverified sync lifecycle command in " + str(identity.core / "package.json")
                             + "; inspect sync and package installation lifecycle scripts"]
            model.blocked = True
            return model
        if re.search(r"(?:--require|--import|--loader|--experimental-loader|-r)(?:=|\s|$)", environ.get("NODE_OPTIONS", "")):
            model.unknown = ["NODE_OPTIONS loads unverified code before the sync script; inspect that environment setting"]
            model.blocked = True
            return model
        if entrypoint == 'pnpm':
            model.writes[identity.core / 'node_modules'] = 'Core frozen package installation'
        existing = literal_config(identity.workspace / '.gclient')
        brave_config = literal_config(identity.core / ".brave_gclient")
        env = dict(environ, SCAFFOLD_GCLIENT_CONFIG=json.dumps(existing),
                   SCAFFOLD_BRAVE_GCLIENT_CONFIG=json.dumps(brave_config))
        env.pop("NODE_OPTIONS", None)
        env.pop("NODE_PATH", None)
        probe = run_capture([str(toolchain.node), '--permission', '--allow-fs-read=*', str(PROBE), str(identity.core),
                             *arguments[2:]], str(identity.core), env, log, timeout=60)
        if probe.returncode or probe.truncated:
            try:
                failure = json.loads(probe.stdout)
            except ValueError:
                failure = {}
            location = failure.get('path') or str(identity.core / '.env')
            model.unknown = ['read-only sync configuration inspection failed at ' + location
                             + '; inspect that path, .gclient, and the local Node payload']
            model.blocked = True
            return model
        config = json.loads(probe.stdout)
        if config['ci'] or Path(config['core_dir']).resolve() != identity.core.resolve() \
                or config['delete_trees'] or config['custom_solutions']:
            model.unknown = ['CI deletion, checkout redirection, or custom gclient settings have an unverified mutation scope']
            model.blocked = True
            return model
        chromium = chromium_will_sync(identity, config, log)
        model.chromium = 'required' if chromium else 'skipped'
        model.resets = set(scope.repositories) - {identity.core} if chromium else set()
        if not config['core_unmanaged']:
            model.resets.add(identity.core)
        # Brave-only gclient has --force but no --reset. Force alone also scrubs
        # dependency work, so --sync_chromium=false --force still needs a guard.
        brave_dependencies = sync_scope._repositories_in(identity.core,
            sync_scope.read_entries(identity.core / sync_scope.CORE_ENTRIES), [], sync_scope.CORE_ENTRIES)
        if not chromium and config['force']:
            model.resets.update(path for path in brave_dependencies if path != identity.core)
        model.writes.pop(default_depot, None)
        model.writes.update({Path(filename): 'Core configuration default creation'
                             for filename in config.get('configuration_files', [])})
        model.writes[identity.src / 'build/config/siso/.sisorc'] = 'Core Siso configuration write'
        model.generated = version_outputs(identity, config['brave_version'], contracts, log)
        depot = Path(config['depot_tools'])
        if (depot / '.git').exists():
            model.repositories.add(depot)
        marker = depot / '.disable_auto_update'
        recorded = marker.read_text().strip() if marker.exists() else None
        recorded = recorded if recorded and re.fullmatch(r'[0-9a-fA-F]{40}', recorded) else None
        if not marker.exists() or recorded != config['depot_ref'] or Path(str(depot) + '_install.guard').exists():
            model.writes[depot] = "depot_tools installation or self-update"
        # Pre-DEPS hooks still run during gclient sync with --nohooks.
        writes, unknown = hook_writes(identity, contracts, regular=config['hooks'])
        model.writes.update(writes)
        model.unknown.extend(unknown)
        if chromium:
            target = gitstate._git(identity.src, ['rev-parse', '--verify', config['chromium_ref'] + '^{commit}'], log)
            if target.returncode or target.stdout.strip() != gitstate.head_commit(identity.src, log):
                model.unknown.append('incoming Chromium hook inventory is unverified; inspect the target revision or use --sync_chromium=false')
        return model
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        model.unknown = ['sync configuration could not be established; inspect .gclient and Core configuration']
        model.blocked = True
        return model


def version_outputs(identity, version, contracts, log=None):
    """Recognize exact version writer output without trusting an existing checkout wholesale."""
    if sha256_or_none(identity.core / 'build/util/version.py') not in contracts['version_writers']:
        return set()
    head = gitstate._git(identity.src, ['show', 'HEAD:chrome/VERSION'], log)
    match = re.fullmatch(r'MAJOR=(\d+)\nMINOR=\d+\nBUILD=\d+\nPATCH=\d+\n', head.stdout)
    if head.returncode or head.truncated or not match or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        return set()
    major, minor, build = version.split('.')
    expected = {'chrome/VERSION': f'MAJOR={match[1]}\nMINOR={major}\nBUILD={minor}\nPATCH={build}\n',
                'chrome/VERSION.chromium': head.stdout}
    return {identity.src / name for name, contents in expected.items()
            if sha256_or_none(identity.src / name) == hashlib.sha256(contents.encode()).hexdigest()}


def hook_writes(identity, contracts, regular=True):
    """Known hook inventories use reviewed write scopes; unknown inventories stay explicit.

    Conditions are conservatively included. No DEPS or hook code is executed.
    """
    writes, unknown = {}, []
    pending = [(identity.src, identity.workspace), (identity.core, identity.core)]
    visited = set()
    while pending:
        repository, base = pending.pop()
        if repository in visited:
            continue
        visited.add(repository)
        deps = repository / 'DEPS'
        if not deps.is_file():
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', SyntaxWarning)
                tree = ast.parse(deps.read_text())
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                names = {target.id for target in node.targets if isinstance(target, ast.Name)}
                if 'use_relative_paths' in names:
                    if ast.literal_eval(node.value):
                        base = repository
                if 'recursedeps' in names:
                    for entry in ast.literal_eval(node.value):
                        if not isinstance(entry, str):
                            unknown.append('custom recursive DEPS filename in ' + str(deps))
                            continue
                        child = (base / entry).resolve()
                        if not child.is_relative_to(identity.src):
                            unknown.append('hook recursion outside checkout: ' + str(child))
                        elif child.is_dir():
                            pending.append((child, base))
            hook_names = {'hooks', 'pre_deps_hooks'} if regular else {'pre_deps_hooks'}
            hook_nodes = [node.value for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id in hook_names
                                  for target in node.targets)]
            for node in hook_nodes:
                key = hashlib.sha256(ast.dump(node).encode()).hexdigest()
                contract = contracts['hooks'].get(key)
                if contract is None:
                    if not isinstance(node, ast.List) or node.elts:
                        unknown.append('unverified hooks in ' + str(deps))
                    continue
                for filename, digest in contract['sources'].items():
                    if sha256_or_none(repository / filename) != digest:
                        unknown.append('unverified hook source ' + str(repository / filename))
                writes.update({Path(os.path.abspath(repository / relative)): "hooks declared in " + str(deps)
                               for relative in contract["writes"]})
        except (OSError, SyntaxError, ValueError, TypeError):
            unknown.append('unreadable hook inventory ' + str(deps))
    return writes, unknown
