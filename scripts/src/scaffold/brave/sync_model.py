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
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from ..common.procs import run_capture
from . import gitstate, sync_scope, incoming_hooks
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
    incoming_trees: dict = field(default_factory=dict)

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
        generated_verified = all(sha256_or_none(identity.core / name) == digest
                                 for name, digest in contracts.get('generated_sources', {}).items()) \
                             and bool(contracts.get('generated_sources'))
        if generated_verified:
            env['SCAFFOLD_INSPECT_GENERATED'] = '1'
        else:
            env.pop('SCAFFOLD_INSPECT_GENERATED', None)
        probe = run_capture([str(toolchain.node), '--permission', '--allow-fs-read=*', str(PROBE), str(identity.core),
                             *arguments[2:]], str(identity.core), env, log, timeout=60, max_bytes=16 << 20)
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
        model.generated.update(devtools_hardlinks(identity, contracts))
        if generated_verified:
            copies = config.get('generated_copies', [])
            model.generated.update(generated_outputs(identity, copies, env, log))
            model.generated.update(previous_branding_copies(identity, copies, model.generated, contracts, log))
        depot = Path(config['depot_tools'])
        if (depot / '.git').exists():
            model.repositories.add(depot)
        marker = depot / '.disable_auto_update'
        recorded = marker.read_text().strip() if marker.exists() else None
        recorded = recorded if recorded and re.fullmatch(r'[0-9a-fA-F]{40}', recorded) else None
        if depot_will_change(depot, recorded, config['depot_ref'], environ, contracts):
            model.writes[depot] = "depot_tools installation or self-update"
        # Pre-DEPS hooks still run during gclient sync with --nohooks.
        writes, unknown = hook_writes(identity, contracts, regular=config['hooks'], variables=config.get('dependency_variables', {}))
        model.writes.update(writes)
        model.unknown.extend(unknown)
        if chromium:
            target = gitstate._git(identity.src, ['rev-parse', '--verify', config['chromium_ref'] + '^{commit}'], log)
            if target.returncode or target.stdout.strip() != gitstate.head_commit(identity.src, log):
                incoming_writes, incoming_unknown = incoming_hooks.inspect(
                    identity, config['chromium_ref'], contracts, regular=config['hooks'], log=log,
                    variables=config.get('dependency_variables', {}), custom_deps=config.get('custom_dependencies', {}), trees_out=model.incoming_trees)
                model.writes.update(incoming_writes)
                model.unknown.extend(incoming_unknown)
        if config.get('lean_sync') or depot in model.writes or sha256_or_none(depot / 'gclient_scm.py') != contracts.get('preserving_reset_source'):
            model.incoming_trees.clear()
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


def hook_writes(identity, contracts, regular=True, variables=None):
    """Known hook inventories use reviewed write scopes; unknown inventories stay explicit.

    Conditions are conservatively included. No DEPS or hook code is executed.
    """
    writes, unknown = {}, []
    pending = [(identity.src, identity.workspace, variables or {}), (identity.core, identity.core, variables or {})]
    visited = set()
    while pending:
        repository, base, inherited = pending.pop()
        if repository in visited:
            continue
        visited.add(repository)
        deps = repository / 'DEPS'
        if not deps.is_file():
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', SyntaxWarning)
                parsed = incoming_hooks.Deps(deps.read_text(), inherited)
                parsed.overrides = parsed.variables()
                tree = parsed.tree
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
                        if not child.is_relative_to(identity.src.resolve()):
                            unknown.append('hook recursion outside checkout: ' + str(child))
                        elif child.is_dir():
                            pending.append((child, base, parsed.overrides))
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
                if parsed.hooks_disabled(node):
                    continue
                for filename, digest in contract['sources'].items():
                    if sha256_or_none(repository / filename) not in (digest if isinstance(digest, list) else [digest]):
                        unknown.append('unverified hook source ' + str(repository / filename))
                writes.update({Path(os.path.abspath(repository / relative)): "hooks declared in " + str(deps)
                               for relative in contract["writes"]})
        except (OSError, SyntaxError, ValueError, TypeError):
            unknown.append('unreadable hook inventory ' + str(deps))
    return writes, unknown


def matching_copies(identity, copies):
    """Only an exact source copy can explain existing output; filenames and headers cannot."""
    result, digests = set(), {}
    def digest(path):
        if path not in digests:
            digests[path] = sha256_or_none(path)
        return digests[path]
    for source, destination in copies:
        source, destination = Path(source), Path(destination)
        if (not source.resolve().is_relative_to(identity.core.resolve())
                or not destination.resolve().is_relative_to(identity.src.resolve())
                or destination.is_symlink() or not source.is_file() or not destination.is_file()):
            continue
        if digest(source) is not None and digest(source) == digest(destination):
            result.add(destination)
    return result


def generated_outputs(identity, copies, environ, log):
    copies = [*copies, (identity.core / 'build/config/siso/brave_siso_config.star',
                        identity.src / 'build/config/siso/brave_siso_config.star')]
    result = matching_copies(identity, copies)
    rendered = run_capture([sys.executable, '-I', '-B', str(Path(__file__).with_name('generated_probe.py')),
                            str(identity.core), str(CONTRACTS)], str(identity.core), environ, log, timeout=30)
    if rendered.returncode == 0 and not rendered.truncated:
        try:
            for filename, digest in json.loads(rendered.stdout).items():
                path = Path(filename)
                if path.resolve().is_relative_to(identity.src.resolve()) and not path.is_symlink() and sha256_or_none(path) == digest:
                    result.add(path)
        except (ValueError, TypeError, AttributeError):
            pass
    return result


def devtools_hardlinks(identity, contracts):
    """Recognize existing sibling .patch.ts hardlinks made by the reviewed wrapper."""
    producer = contracts.get('devtools_hardlink_source', {})
    if not producer or sha256_or_none(identity.core / producer['path']) != producer['sha256']:
        return set()
    result = set()
    base = identity.core / 'chromium_src'
    mappings = [(source, identity.src / source.relative_to(identity.core))
                for source in (identity.core / 'third_party/devtools-frontend/src').rglob('*.ts')
                if not source.name.endswith('.d.ts')]
    mappings.extend((source, (identity.src / source.relative_to(base)).with_suffix('.patch.ts'))
                    for source in (base / 'third_party/devtools-frontend/src').rglob('*.ts')
                    if not source.name.endswith('.d.ts'))
    for source, target in mappings:
        if source.is_symlink() or target.is_symlink():
            continue
        try:
            if source.samefile(target):
                result.add(target)
        except OSError:
            continue
    return result


def previous_branding_copies(identity, copies, known, contracts, log=None):
    """Recognize old copies only from a prior checkout for the last synced Chromium ref.

    Use the first matching reflog revision, not a search for any historical bytes.
    Both the successful-sync ref and the unchanged reviewed copier are required.
    """
    try:
        receipt = json.loads((identity.workspace / '.brave_latest_successful_sync.json').read_text())
        ref = receipt['chromiumRef']
        target = gitstate._git(identity.src, ['rev-parse', '--verify', ref + '^{commit}'], log)
        if target.returncode or target.truncated or target.stdout.strip() != gitstate.head_commit(identity.src, log):
            return set()
        history = gitstate._git(identity.core, ['reflog', '-32', '--format=%H'], log)
        if history.returncode or history.truncated:
            return set()
        revision = None
        for candidate in dict.fromkeys(history.stdout.splitlines()):
            package = gitstate._git(identity.core, ['show', candidate + ':package.json'], log)
            if package.returncode or package.truncated:
                continue
            project = json.loads(package.stdout)['config']['projects']['chrome']
            if ref == 'refs/tags/' + project.get('tag', ''):
                revision = candidate
                break
        if revision is None:
            return set()
        for name in ('build/commands/lib/branding.js', 'build/commands/lib/l10nUtil.js'):
            source = gitstate._git(identity.core, ['show', revision + ':' + name], log)
            if source.returncode or source.truncated or hashlib.sha256(source.stdout.encode()).hexdigest() != contracts['generated_sources'][name]:
                return set()
        result = set()
        for source, destination in copies:
            source, destination = Path(source), Path(destination)
            if destination in known or destination.is_symlink() or not destination.is_file():
                continue
            if not source.resolve().is_relative_to(identity.core.resolve()) or not destination.resolve().is_relative_to(identity.src.resolve()):
                continue
            blob = gitstate._git(identity.core, ['rev-parse', '--verify', revision + ':' + str(source.relative_to(identity.core))], log)
            actual = gitstate._git(identity.src, ['hash-object', '--no-filters', '--', str(destination)], log)
            if not blob.returncode and not actual.returncode and not blob.truncated and not actual.truncated and blob.stdout.strip() == actual.stdout.strip():
                result.add(destination)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def depot_will_change(depot, recorded, requested, environ, contracts):
    reviewed = contracts.get('depot_update_sources', {})
    update_disabled = (environ.get('DEPOT_TOOLS_UPDATE') == '0' and bool(reviewed)
                       and all(sha256_or_none(depot / name) == digest for name, digest in reviewed.items()))
    return (not depot.is_dir() or recorded != requested
            or Path(str(depot) + '_install.guard').exists()
            or (not (depot / '.disable_auto_update').exists() and not update_disabled))
