// Copyright (c) 2026 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

// Runs under Node's permission model with read permission only. Configuration
// default creation is captured; no checkout writes are allowed.
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { createRequire } from 'node:module'

try {
const [core, ...forwarded] = process.argv.slice(2)
const require = createRequire(path.join(core, 'package.json'))
const { Command } = require('commander')
const program = new Command().exitOverride().configureOutput({ writeOut() {}, writeErr() {} })
program
  .option('--gclient_verbose')
  .option('--target_os <value>')
  .option('--target_arch <value>')
  .option('--init').option('--force').option('--no-history').option('--no-bootstrap')
  .option('--fetch_all').option('-C, --sync_chromium [arg]', '', JSON.parse)
  .option('-D, --delete_unused_deps').option('--nohooks').option('--lean_sync')
program.parse(forwarded, { from: 'user' })
const options = program.opts()
const load = async name => (await import(pathToFileURL(path.join(core, 'build/commands/lib', name)))).default
// Core's status messages do not belong in the structured result, and could
// contain configuration values. Only the selected public fields are returned.
console.log = () => {}
console.warn = () => {}
console.error = () => {}
const configurationFiles = []
// These constructor writes create defaults and directories. Capture them
// without touching disk; every other filesystem write remains denied.
fs.writeFileSync = (filename) => {
  if (path.resolve(filename) !== path.join(core, '.env')) {
    const error = new Error('Unreviewed configuration write')
    error.code = 'READ_ONLY_CONFIGURATION_WRITE'
    error.resource = String(filename)
    throw error
  }
  configurationFiles.push(path.resolve(filename))
}
fs.mkdirSync = () => {}
const config = await load('config.ts')
const { isCI } = await import(pathToFileURL(path.join(core, 'build/commands/lib/ciDetect.ts')))
const util = await load('util.js')
const syncUtil = await load('syncUtils.js')
const existing = JSON.parse(process.env.SCAFFOLD_GCLIENT_CONFIG)
const targets = options.target_os?.split(',').filter(Boolean) || (options.init ? [] : existing.target_os || [])
const arches = options.target_arch?.split(',').filter(Boolean) || (options.init ? [] : existing.target_cpu || [])
if (targets.length) options.target_os = targets[0]
if (arches.length) options.target_arch = arches[0]
config.update(options)
const generated = {}
util.writeFileIfModified = (filename, content) => { generated[filename] = content; return false }
if (!(config.disableGclientConfigUpdate && fs.existsSync(config.gclientFile) && !options.init)) {
  syncUtil.writeGclientConfig(targets, arches)
}
const changed = Object.entries(generated).some(([filename, contents]) =>
  !fs.existsSync(filename) || fs.readFileSync(filename, 'utf8') !== contents)
const gclientChanged = generated[config.gclientFile] !== undefined
  && (!fs.existsSync(config.gclientFile) || fs.readFileSync(config.gclientFile, 'utf8') !== generated[config.gclientFile])
const retained = config.disableGclientConfigUpdate && fs.existsSync(config.gclientFile) && !options.init
const solutions = retained ? existing.solutions : config.gclientGlobalVars.solutions
const braveConfig = JSON.parse(process.env.SCAFFOLD_BRAVE_GCLIENT_CONFIG)
const unsafeRetained = retained && (
  existing.delete_unversioned_trees || braveConfig.delete_unversioned_trees
  || existing.solutions?.some(s => !['src', 'src/brave'].includes(s.name) || s.managed !== false
    || Object.keys(s).some(k => !['name', 'url', 'managed', 'custom_deps', 'custom_vars'].includes(k)))
  || braveConfig.solutions?.some(s => s.name !== '.' || s.managed !== false
    || Object.keys(s).some(k => !['name', 'url', 'managed', 'custom_deps', 'custom_vars'].includes(k)))
)
const customGlobals = Object.keys(config.gclientGlobalVars).some(k => !['cache_dir', 'target_os', 'target_cpu'].includes(k))
process.stdout.write(JSON.stringify({
  chromium_ref: config.getProjectRef('chrome'),
  gclient_changed: gclientChanged,
  gclient_timestamp: fs.existsSync(config.gclientFile) ? fs.statSync(config.gclientFile).mtimeMs.toString() : null,
  configuration_changed: changed,
  ci: isCI,
  core_dir: config.braveCoreDir,
  configuration_files: configurationFiles,
  chromium_option: options.sync_chromium ?? null,
  force: Boolean(options.init || options.force),
  hooks: !options.nohooks,
  core_unmanaged: !retained || (existing.solutions?.some(s => s.name === 'src/brave' && s.managed === false)
    && braveConfig.solutions?.some(s => s.name === '.' && s.managed === false)),
  delete_trees: Boolean(config.gclientGlobalVars.delete_unversioned_trees),
  custom_solutions: Boolean(solutions && !retained) || customGlobals || Boolean(unsafeRetained),
  brave_version: config.braveVersion,
  depot_tools: config.depotToolsDir,
  depot_ref: config.getProjectRef('depot_tools', null),
}) + '\n')

} catch (error) {
  process.stdout.write(JSON.stringify({ inspection_error: error.code || 'configuration error',
    path: ['ERR_ACCESS_DENIED', 'READ_ONLY_CONFIGURATION_WRITE'].includes(error.code) ? error.resource : null }) + '\n')
  process.exitCode = 2
}
