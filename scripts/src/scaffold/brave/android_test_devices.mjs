// Copyright (c) 2026 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.
// Repeat Core's generated runner command while leaving its build and setup intact.

import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const configPath = process.env.SCAFFOLD_ANDROID_TEST_DEVICES
if (configPath) {
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'))
  if (process.argv[1] && path.resolve(process.argv[1]) === config.script) {
    const { default: util } = await import(pathToFileURL(config.util).href)
    const run = util.run
    const report = { runs: [] }
    const save = () => {
      const temporary = config.report + '.tmp'
      fs.writeFileSync(temporary, JSON.stringify(report))
      fs.renameSync(temporary, config.report)
    }
    save()
    util.run = (command, args = [], options = {}) => {
      const resolved = path.resolve(options.cwd || process.cwd(), command)
      if (resolved !== config.runner) {
        if (path.dirname(resolved) === path.dirname(config.runner) && path.basename(resolved).startsWith('run_')) {
          throw new Error('The test command selected an unexpected runner: ' + resolved)
        }
        return run(command, args, options)
      }
      if (report.runs.length) throw new Error('The test command invoked its runner more than once')
      let outcome
      for (const device of config.devices) {
        const deviceArgs = []
        for (let index = 0; index < args.length; index++) {
          const name = args[index].split('=')[0]
          if (name === '--device' || name === '--json-results-file') {
            if (!args[index].includes('=')) index++
          } else {
            deviceArgs.push(args[index])
          }
        }
        deviceArgs.push('--device', device.id, '--json-results-file=' + device.results)
        fs.rmSync(device.results, { force: true })
        const entry = { device: device.id, argv: [command, ...deviceArgs],
          cwd: options.cwd || process.cwd(), results: device.results, status: 'running' }
        report.runs.push(entry)
        save()
        console.log('Running tests on ' + device.id + '...')
        outcome = run(command, deviceArgs, { ...options, continueOnFail: true })
        entry.exit = outcome.status
        entry.signal = outcome.signal || null
        entry.status = 'finished'
        save()
        if (outcome.signal) {
          process.kill(process.pid, outcome.signal)
          break
        }
      }
      return outcome
    }
  }
}
