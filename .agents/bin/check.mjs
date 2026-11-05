#!/usr/bin/env node
// Node 20-compatible runner and coverage reporter. No dependencies or GitHub access.
// Event schema: https://nodejs.org/download/release/v20.19.0/docs/api/test.html#event-testcoverage
import assert from 'node:assert/strict';
import { readdirSync, realpathSync } from 'node:fs';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const filename = fileURLToPath(import.meta.url);
const root = resolve(dirname(filename), '../..');

export function requiredCoverageFiles(base = root) {
  const lib = resolve(base, '.agents/lib');
  const modules = readdirSync(lib).filter(name => name.endsWith('.mjs')).sort();
  assert(modules.length, 'No framework library modules found');
  return [...modules.map(name => resolve(lib, name)), ...['engineering.mjs', 'check.mjs'].map(name => resolve(base, '.agents/bin', name))];
}

export function coverageFailures(summary, required) {
  assert(required.length, 'Coverage must include production files');
  const files = new Map((summary?.files || []).map(file => [resolve(file.path), file]));
  const failures = [];
  for (const path of required) {
    const file = files.get(resolve(path));
    if (!file) { failures.push(`${path}: missing measured coverage`); continue; }
    for (const kind of ['Line', 'Branch']) {
      const total = file[`total${kind}Count`];
      const covered = file[`covered${kind}Count`];
      // Use counts, not rounded percentages: exactly 95% is insufficient.
      if (!Number.isInteger(total) || total <= 0 || !Number.isInteger(covered)
          || covered < 0 || covered > total || covered / total <= 0.95) {
        failures.push(`${path}: ${kind.toLowerCase()} coverage must exceed 95% (${covered}/${total})`);
      }
    }
  }
  return failures;
}

export default async function* coverageReporter(events) {
  let summary;
  for await (const event of events) {
    if (event.type === 'test:coverage') summary = event.data.summary;
  }
  const required = requiredCoverageFiles();
  const failures = coverageFailures(summary, required);
  for (const file of summary?.files || []) {
    if (!required.includes(resolve(file.path))) continue;
    yield `Framework coverage ${relative(root, file.path)}: lines ${file.coveredLineCount}/${file.totalLineCount}, branches ${file.coveredBranchCount}/${file.totalBranchCount}\n`;
  }
  if (failures.length) {
    process.exitCode = 1;
    yield `Framework coverage gate failed:\n${failures.join('\n')}\n`;
    return;
  }
  yield 'Framework coverage gate: every production module and CLI exceeds 95% lines and branches.\n';
}

export function runChecks(args = [], {
  base = root, nodeVersion = process.versions.node, executable = process.execPath,
  spawn = spawnSync, stderr = process.stderr,
} = {}) {
  assert(Number(nodeVersion.split('.')[0]) >= 20, 'Framework checks require Node.js 20 or newer');
  assert.equal(args.length, 0, 'Usage: node .agents/bin/check.mjs');
  const tests = readdirSync(resolve(base, '.agents/tests')).filter(name => name.endsWith('.test.mjs')).sort();
  assert(tests.length, 'No framework tests found');
  const result = spawn(executable, [
    '--test', '--experimental-test-coverage',
    '--test-reporter=spec', '--test-reporter-destination=stdout',
    `--test-reporter=${resolve(base, '.agents/bin/check.mjs')}`, '--test-reporter-destination=stdout',
    ...tests.map(name => resolve(base, '.agents/tests', name)),
  ], { cwd: base, stdio: 'inherit' });
  if (result.error) stderr.write(`${result.error.message}\n`);
  return result.status ?? 1;
}

if (process.argv[1] && realpathSync(process.argv[1]) === filename) process.exitCode = runChecks(process.argv.slice(2));
