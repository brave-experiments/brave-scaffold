# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Inspect incoming hook inventories in local Git objects without checking them out."""
import ast
import hashlib
import os
from pathlib import Path
import warnings

from . import gitstate, sync_scope
from .patch_inventory import sha256_or_none


class Unavailable(ValueError):
    pass


class Deps:
    """Read only the literal/string expressions needed to resolve recursive Git pins."""
    def __init__(self, text, overrides=None):
        self.overrides = overrides or {}
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            self.tree = ast.parse(text)
        self.assignments = {target.id: node.value for node in self.tree.body if isinstance(node, ast.Assign)
                            for target in node.targets if isinstance(target, ast.Name)}

    def evaluate(self, node, depth=0):
        if depth > 20:
            raise Unavailable('recursive DEPS expression')
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.evaluate(node.left, depth+1) + self.evaluate(node.right, depth+1)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1 and not node.keywords:
            if node.func.id == 'Var':
                name = self.evaluate(node.args[0], depth+1)
                return self.overrides[name] if name in self.overrides else self.item('vars', name, depth+1)
            if node.func.id == 'Str':
                return self.evaluate(node.args[0], depth+1)
        return ast.literal_eval(node)

    def item(self, name, key, depth=0):
        node = self.assignments.get(name)
        if not isinstance(node, ast.Dict):
            raise Unavailable('missing literal ' + name)
        for k, value in zip(node.keys, node.values):
            if self.evaluate(k, depth+1) == key:
                return self.evaluate(value, depth+1)
        raise Unavailable('missing dependency pin: ' + key)

    def enabled(self, key, variables):
        node = self.assignments.get('deps')
        if not isinstance(node, ast.Dict):
            raise Unavailable('missing dependency map')
        for k, value in zip(node.keys, node.values):
            if self.evaluate(k) != key or not isinstance(value, ast.Dict):
                continue
            for name, condition in zip(value.keys, value.values):
                if self.evaluate(name) == 'condition':
                    return self.condition(ast.parse(self.evaluate(condition), mode='eval').body, variables)
        return True

    def condition(self, node, variables):
        if isinstance(node, ast.Name):
            value = variables[node.id] if node.id in variables else self.item('vars', node.id)
            if isinstance(value, ast.AST):
                raise Unavailable('unresolved variable: ' + node.id)
            return value
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not self.condition(node.operand, variables)
        if isinstance(node, ast.BoolOp):
            values = (self.condition(value, variables) for value in node.values)
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = self.condition(node.left, variables), self.condition(node.comparators[0], variables)
            if isinstance(node.ops[0], ast.Eq): return left == right
            if isinstance(node.ops[0], ast.NotEq): return left != right
        raise Unavailable('unsupported dependency condition')

    def variables(self):
        effective = {}
        defaults = self.assignments.get('vars')
        if isinstance(defaults, ast.Dict):
            for key, value in zip(defaults.keys, defaults.values):
                name = self.evaluate(key)
                try:
                    effective[name] = self.evaluate(value)
                except (ValueError, TypeError):
                    effective[name] = value
        effective.update(self.overrides)
        return effective

    def hooks_disabled(self, node):
        if not isinstance(node, ast.List):
            return False
        try:
            for hook in node.elts:
                if not isinstance(hook, ast.Dict):
                    return False
                conditions = [value for key, value in zip(hook.keys, hook.values)
                              if self.evaluate(key) == 'condition']
                if len(conditions) != 1 or self.condition(
                        ast.parse(self.evaluate(conditions[0]), mode='eval').body, self.overrides):
                    return False
            return True
        except (ValueError, TypeError, KeyError, SyntaxError):
            return False

    def pin(self, key):
        # A deps entry may be a string or a dictionary containing a URL and condition.
        node = self.assignments.get('deps')
        if not isinstance(node, ast.Dict):
            raise Unavailable('missing dependency map')
        for k, value in zip(node.keys, node.values):
            if self.evaluate(k) != key:
                continue
            if isinstance(value, ast.Dict):
                value = next((v for k,v in zip(value.keys,value.values) if self.evaluate(k) == 'url'), None)
            url = self.evaluate(value) if value is not None else None
            if not isinstance(url, str) or '@' not in url:
                raise Unavailable('dependency has no Git revision: ' + key)
            return url.rsplit('@', 1)[1]
        raise Unavailable('missing dependency pin: ' + key)


class Trees:
    def __init__(self, identity, revision, log):
        self.identity, self.log = identity, log
        self.revisions = {identity.src: revision}
        self.readers = {}
        self.contents = {}

    def git(self, repo, args):
        result = gitstate._git(repo, args, self.log)
        if result.returncode or result.truncated:
            raise Unavailable('Git cannot inspect ' + str(repo) + ': ' + ' '.join(args))
        return result.stdout

    def reader(self, repo):
        if repo not in self.readers:
            revision = self.revisions[repo]
            try:
                self.git(repo, ['rev-parse', '--verify', revision + '^{commit}'])
                reader = repo
            except Unavailable:
                # A local cache is read-only here. Never fetch, change refs, or guess a URL-to-cache mapping.
                remote = Path(self.git(repo, ['remote', 'get-url', 'origin']).strip())
                if not remote.is_absolute() or not remote.is_dir():
                    raise Unavailable('incoming revision unavailable locally: ' + str(repo) + '@' + revision)
                try:
                    self.git(remote, ['rev-parse', '--verify', revision + '^{commit}'])
                except Unavailable:
                    raise Unavailable('incoming revision unavailable locally: ' + str(repo) + '@' + revision)
                reader = remote
            self.readers[repo] = reader
        return self.readers[repo]

    def read(self, repo, relative, revision=None):
        ref = revision or self.revisions[repo]
        key = (repo, relative, ref)
        if key not in self.contents:
            self.contents[key] = self.git(self.reader(repo), ['show', ref + ':' + relative])
        return self.contents[key]

    def source_matches(self, owner, filename, digest):
        path = Path(os.path.abspath(owner / filename))
        # Hook sources can live in another dependency repository. Its pin is resolved by the caller.
        candidates = [repo for repo in self.revisions if path.is_relative_to(repo)]
        repo = max(candidates, key=lambda p: len(p.parts))
        relative = str(path.relative_to(repo))
        incoming = self.read(repo, relative)
        digests = digest if isinstance(digest, list) else [digest]
        if hashlib.sha256(incoming.encode()).hexdigest() in digests:
            return True
        # Unchanged upstream input plus the reviewed local patched source is also known.
        return (sha256_or_none(path) in digests
                and incoming == self.git(repo, ['show', 'HEAD:' + relative]))


def inspect(identity, revision, contracts, regular=True, log=None, variables=None, custom_deps=None, trees_out=None):
    """Return writes and uncertainties for the incoming Chromium/dependency hook tree."""
    variables, custom_deps = variables or {}, custom_deps or {}
    trees = Trees(identity, revision, log)
    pending = [(identity.src, identity.workspace, variables)]
    inventories, problems = [], []
    visited = set()
    scope = sync_scope.sync_repositories(identity)
    try:
        while pending:
            repo, base, inherited = pending.pop()
            if repo in visited:
                continue
            visited.add(repo)
            deps = Deps(trees.read(repo, 'DEPS'), inherited)
            effective = deps.variables()
            deps.overrides = effective
            if 'use_relative_paths' in deps.assignments and deps.evaluate(deps.assignments['use_relative_paths']):
                base = repo
            # Resolve installed direct dependencies too: hook scripts often live outside recursedeps.
            for child in scope.repositories:
                if child in trees.revisions or child == identity.core or not child.is_relative_to(base):
                    continue
                try:
                    trees.revisions[child] = deps.pin(str(child.relative_to(base)))
                except (ValueError, TypeError):
                    pass
            recursive = deps.evaluate(deps.assignments.get('recursedeps', ast.List(elts=[])))
            for entry in recursive:
                if not isinstance(entry, str):
                    raise Unavailable('custom recursive DEPS file in ' + str(repo))
                if entry in custom_deps:
                    if custom_deps[entry] is None:
                        continue
                    raise Unavailable('custom recursive dependency revision: ' + entry)
                if not deps.enabled(entry, effective):
                    continue
                child = Path(os.path.abspath(base / entry))
                if not child.resolve().is_relative_to(identity.src.resolve()):
                    raise Unavailable('recursive dependency leaves checkout')
                if not child.is_dir():
                    raise Unavailable('incoming recursive dependency not available locally: ' + str(child))
                trees.revisions[child] = deps.pin(entry)
                pending.append((child, base, effective))
            inventories.append((repo, deps))
        writes = {}
        for repo, deps in inventories:
            for name in ('hooks', 'pre_deps_hooks') if regular else ('pre_deps_hooks',):
                node = deps.assignments.get(name)
                if node is None or isinstance(node, ast.List) and not node.elts:
                    continue
                key = hashlib.sha256(ast.dump(node).encode()).hexdigest()
                contract = contracts['hooks'].get(key)
                if contract is None:
                    problems.append('unreviewed incoming hooks: ' + str(repo / 'DEPS'))
                    continue
                if deps.hooks_disabled(node):
                    continue
                for filename, digest in contract['sources'].items():
                    if not trees.source_matches(repo, filename, digest):
                        problems.append('unreviewed incoming hook source: ' + str(repo / filename))
                writes.update({Path(os.path.abspath(repo / path)): 'incoming hooks declared in ' + str(repo / 'DEPS')
                               for path in contract['writes']})
        if trees_out is not None and not problems:
            for repo, ref in trees.revisions.items():
                try:
                    trees_out[repo] = (trees.reader(repo), ref)
                except Unavailable:
                    pass
        return writes, problems
    except (Unavailable, OSError, ValueError, TypeError, SyntaxError) as error:
        return {}, ['incoming hook inspection incomplete: ' + str(error)]
