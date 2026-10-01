# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Render reviewed reclient configurations in memory; never run setup or hooks."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys


def deny_mutation(event, args):
    if event == 'open':
        mode, flags = args[1:3]
        if (mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise PermissionError('The generated-file probe permits reads only')
    if event.startswith(('subprocess.', 'socket.', 'os.exec', 'os.spawn')) or event in {
        'os.system', 'os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.link', 'os.symlink',
        'os.truncate', 'os.chmod', 'os.chown', 'os.utime', 'os.chdir', 'os.fork', 'os.posix_spawn',
    }:
        raise PermissionError('The generated-file probe permits reads only')


def main():
    core, contract_path = map(Path, sys.argv[1:])
    contract = json.loads(contract_path.read_text())['generated_sources']
    generator = 'third_party/reclient_configs/src/configure_reclient.py'
    custom = 'third_party/reclient_configs/brave_custom/brave_custom.py'
    for name in (generator, custom):
        if hashlib.sha256((core / name).read_bytes()).hexdigest() != contract.get(name):
            raise ValueError('Unreviewed reclient generator')
    sys.addaudithook(deny_mutation)
    module = runpy.run_path(str(core / generator))
    sys.argv = [generator, '--src_dir', str(core.parent), '--custom_py', str(core / custom)]
    args = module['parse_args']()
    module['Paths'].init_from_args(args)
    outputs = {}
    def capture(filename, contents):
        outputs[str(filename)] = hashlib.sha256(contents.encode()).hexdigest()
    module['FileUtils'].write_text_file = staticmethod(capture)
    runner = module['ReclientConfigurator'](args)
    runner.load_custom_py()
    runner.run_custom_py_pre_configure()
    # Only pure rendering, never configure(), downloads, or post hooks.
    runner.generate_rewrapper_cfgs()
    print(json.dumps(outputs))


if __name__ == '__main__':
    main()
