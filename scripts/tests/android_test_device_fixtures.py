# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""A small Core test-command substitute for the real Node device adapter."""

NOTICE = """// Copyright (c) 2026 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.
"""

SCRIPT = NOTICE + """
import fs from 'node:fs'
import util from '../lib/util.js'
const config = JSON.parse(fs.readFileSync(process.env.SCAFFOLD_ANDROID_TEST_DEVICES))
fs.appendFileSync(process.env.FAKE_RECORD, JSON.stringify({tool: 'test-build'}) + '\\n')
if (process.env.FAKE_MULTI_BUILD_FAIL) process.exit(7)
const args = ['--enable-logging=stderr', '--gtest_filter=Example.*', '--test-launcher-jobs=4',
  '--device', config.devices[0].id, '--adb-path', '/fake/adb', '--json-results-file=/unused.json']
util.run(config.runner, args, {cwd: process.env.FAKE_TEST_OUTPUT, env: {...process.env, CORE_TEST_SETUP: 'preserved'}})
"""

UTIL = NOTICE + """
import fs from 'node:fs'
export default {run(command, args, options) {
  const serial = args[args.indexOf('--device') + 1]
  const result = args.find(arg => arg.startsWith('--json-results-file=')).split('=')[1]
  fs.appendFileSync(process.env.FAKE_RECORD, JSON.stringify({tool: 'test-runner', argv: [command, ...args],
    device: serial, setup: options.env.CORE_TEST_SETUP}) + '\\n')
  const failed = serial === process.env.FAKE_MULTI_FAIL_DEVICE
  if (serial !== process.env.FAKE_MULTI_NO_RESULTS) {
    fs.writeFileSync(result, JSON.stringify({per_iteration_data: [{test: [{status: failed ? 'FAILURE' : 'SUCCESS'}]}]}))
  }
  return {status: failed && !process.env.FAKE_MULTI_ZERO_EXIT ? 9 : 0, signal: null}
}}
"""
