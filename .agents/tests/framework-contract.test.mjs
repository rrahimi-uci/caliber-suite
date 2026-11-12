import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync, readdirSync, realpathSync, existsSync, mkdtempSync, mkdirSync, writeFileSync, copyFileSync, rmSync } from 'node:fs';
import { dirname, resolve, relative, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import * as checker from '../bin/check.mjs';
const { coverageFailures, requiredCoverageFiles } = checker;

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const read = path => readFileSync(resolve(root, path), 'utf8');
const routes = {
  architect: ['architecture', 'architect'], plan: ['planning', 'planner'],
  develop: ['development', 'developer'], review: ['review', 'reviewer'],
  critique: ['milestone-critique', 'critic'], 'bug-bash': ['bug-bash', 'bug-basher'],
  'release-check': ['release-check', 'qa'],
};

function markdownFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? markdownFiles(path) : entry.name.endsWith('.md') ? [path] : [];
  });
}

test('all seven commands route to one canonical workflow and role after live Project context', () => {
  assert.deepEqual(readdirSync(resolve(root, '.agents/commands')).filter(n => n.endsWith('.md')).sort(),
    Object.keys(routes).map(n => `${n}.md`).sort());
  for (const [command, [workflow, role]] of Object.entries(routes)) {
    const text = read(`.agents/commands/${command}.md`);
    const context = text.indexOf(`node .agents/bin/engineering.mjs context ${command} <target>`);
    const route = text.indexOf(`../workflows/${workflow}.md`);
    assert(context >= 0 && route > context, `${command}: live context must precede workflow execution`);
    assert(text.includes('../engineering-policy.md'));
    assert(text.includes('../github/project-integration.md'));
    assert(text.includes(`../roles/${role}.md`));
    assert.equal([...text.matchAll(/\.\.\/workflows\/[^)\s]+\.md/g)].length, 1);
    assert(existsSync(resolve(root, `.agents/workflows/${workflow}.md`)));
    assert(existsSync(resolve(root, `.agents/roles/${role}.md`)));
  }
});

test('Codex and Claude entry points and adapters route to shared policy without duplicate workflows', () => {
  for (const path of ['AGENTS.md', 'CLAUDE.md']) {
    const text = read(path);
    assert(text.includes('.agents/engineering-policy.md'));
    assert(text.includes('.agents/README.md'));
    assert.match(text, /live Project\s+context/);
    assert(text.length < 4000, `${path}: keep the entry point thin`);
  }
  assert.match(read('AGENTS.md'), /not registered native/);
  assert.deepEqual(readdirSync(resolve(root, '.claude/commands')).filter(n => n.endsWith('.md')).sort(),
    Object.keys(routes).map(n => `${n}.md`).sort());
  for (const command of Object.keys(routes)) {
    const text = read(`.claude/commands/${command}.md`);
    assert(text.includes(`.agents/commands/${command}.md`));
    assert(text.includes('.agents/engineering-policy.md'));
    assert(text.includes('$ARGUMENTS'));
    assert(text.length < 1500, `${command}: adapter must delegate to the canonical command`);
    assert.doesNotMatch(text, /^## /m, 'adapter must not redefine workflow sections');
  }
});

test('framework documentation local links exist and stay within the repository', () => {
  // Agent worktrees under .claude/worktrees are independent checkouts and may
  // legitimately be at an older revision. They are not framework documentation
  // and must not make the canonical checkout's link gate nondeterministic.
  const files = [...markdownFiles(resolve(root, '.agents')), ...markdownFiles(resolve(root, '.claude/commands')),
    resolve(root, 'AGENTS.md'), resolve(root, 'CLAUDE.md')];
  let count = 0;
  for (const file of files) {
    const text = readFileSync(file, 'utf8');
    for (const [, raw] of text.matchAll(/\[[^\]\n]*\]\(([^\n)]+)\)/g)) {
      const target = raw.split('#')[0];
      if (!target || /^[a-z][a-z\d+.-]*:/i.test(target)) continue;
      const path = resolve(dirname(file), decodeURIComponent(target));
      assert(!relative(root, path).startsWith('..'), `${file}: link leaves repository: ${raw}`);
      assert(existsSync(path), `${file}: broken local link: ${raw}`);
      count++;
    }
  }
  assert(count > 20, 'routing links must actually be inspected');
});

test('CI and local docs jobs execute the same framework gate from repository root', () => {
  const ci = read('.github/workflows/ci.yml').split('\n  docs-validation:\n')[1].split('\n  compose:')[0];
  assert.match(ci, /working-directory: \$\{\{ github\.workspace \}\}\s+run: node \.agents\/bin\/check\.mjs/);
  const local = read('scripts/ci-local.sh').split('job_docs_validation() {')[1].split('\n}')[0];
  assert(local.indexOf('cd "$REPO_ROOT"') < local.indexOf('node .agents/bin/check.mjs'));
  assert(local.includes('node .agents/bin/check.mjs || return 1'));
});

test('coverage policy rejects absent, invalid, rounded and exactly-95-percent measurements per file', () => {
  const file = resolve(root, '.agents/lib/example.mjs');
  const good = { path: file, totalLineCount: 100, coveredLineCount: 96, totalBranchCount: 100, coveredBranchCount: 96 };
  const assess = record => coverageFailures({ files: [record] }, [file]);
  assert.deepEqual(assess(good), []);
  assert(coverageFailures(undefined, [file])[0].includes('missing measured coverage'));
  assert.throws(() => coverageFailures({ files: [] }, []), /production files/);
  for (const kind of ['Line', 'Branch']) {
    for (const [covered, total] of [[95, 100], [0, 0], [undefined, 100], [96.5, 100], [101, 100], [-1, 100], [96, NaN], [96, Infinity]]) {
      assert(assess({ ...good, [`covered${kind}Count`]: covered, [`total${kind}Count`]: total,
        [`covered${kind}Percent`]: 100 }).length, `${kind}: do not trust rounded percent or invalid counts`);
    }
    assert.deepEqual(assess({ ...good, [`covered${kind}Count`]: 9501, [`total${kind}Count`]: 10000 }), []);
  }
  assert(coverageFailures({ files: [good] }, [file, `${file}.missing`]).length);
});

// These synthetic events test reporter decisions only. The subprocess gate below
// always consumes Node's actual coverage events; no paths/counts are substituted.
async function exerciseChecker(api, base) {
  const required = api.requiredCoverageFiles(base);
  assert(required.includes(resolve(base, '.agents/bin/check.mjs')));
  assert(required.includes(resolve(base, '.agents/bin/engineering.mjs')));
  const measured = path => ({ path, totalLineCount: 100, coveredLineCount: 96, totalBranchCount: 100, coveredBranchCount: 96 });
  const good = { files: [...required.map(measured), measured(resolve(base, 'unrelated.test.mjs'))] };
  const beforeExit = process.exitCode;
  const report = async events => {
    let output = '';
    for await (const text of api.default(events)) output += text;
    return output;
  };
  try {
    process.exitCode = 0;
    let output = await report([{ type: 'test:pass', data: {} }, { type: 'test:coverage', data: { summary: good } }]);
    assert.match(output, /Framework coverage gate: every production/);
    assert.equal(process.exitCode, 0);
    assert(!output.includes('unrelated.test.mjs'));
    assert.equal((output.match(/Framework coverage \.agents\//g) || []).length, required.length);
    const bad = { files: good.files.map(f => ({ ...f, coveredBranchCount: 95 })) };
    output = await report([{ type: 'test:coverage', data: { summary: bad } }]);
    assert.match(output, /Framework coverage gate failed/);
    assert.match(output, /branch coverage must exceed 95%/);
    assert.equal(process.exitCode, 1);
    process.exitCode = 0;
    output = await report([{ type: 'test:diagnostic', data: { message: 'no measurement' } }]);
    assert.match(output, /missing measured coverage/);
    assert.equal(process.exitCode, 1);
    assert.deepEqual(api.coverageFailures(good, required), []);
    assert(api.coverageFailures({ files: [] }, required).length);
    assert.throws(() => api.coverageFailures(good, []), /production files/);
    for (const kind of ['Line', 'Branch']) {
      for (const [covered, total] of [[95, 100], [0, 0], [undefined, 100], [96.5, 100], [101, 100], [-1, 100], [96, NaN]]) {
        const file = { ...good.files[0], [`covered${kind}Count`]: covered, [`total${kind}Count`]: total };
        assert(api.coverageFailures({ files: [file] }, [file.path]).length);
      }
    }
    const calls = [];
    const spawn = (...args) => { calls.push(args); return { status: 0 }; };
    assert.equal(api.runChecks(undefined, { spawn }), 0);
    const [executable, args, options] = calls[0];
    assert.equal(executable, process.execPath);
    assert.deepEqual(options, { cwd: base, stdio: 'inherit' });
    assert.deepEqual(args.slice(0, 6), ['--test', '--experimental-test-coverage',
      '--test-reporter=spec', '--test-reporter-destination=stdout',
      `--test-reporter=${resolve(base, '.agents/bin/check.mjs')}`, '--test-reporter-destination=stdout']);
    assert.deepEqual(args.slice(6), readdirSync(resolve(base, '.agents/tests')).filter(n => n.endsWith('.test.mjs')).sort().map(n => resolve(base, '.agents/tests', n)));
    assert.throws(() => api.runChecks(['--skip'], { spawn }), /Usage/);
    assert.throws(() => api.runChecks([], { spawn, nodeVersion: '18.0.0' }), /Node.js 20/);
    assert.equal(api.runChecks([], { spawn: () => ({ status: 7 }), executable: '/test/node', nodeVersion: '20.19.0' }), 7);
    assert.equal(api.runChecks([], { spawn: () => ({ status: null, signal: 'SIGTERM' }) }), 1);
    let error = '';
    assert.equal(api.runChecks([], { spawn: () => ({ error: new Error('spawn failed') }), stderr: { write: text => { error += text; } } }), 1);
    assert.equal(error, 'spawn failed\n');
    const empty = mkdtempSync(join(tmpdir(), 'caliber-no-tests-'));
    try {
      mkdirSync(join(empty, '.agents/tests'), { recursive: true });
      writeFileSync(join(empty, '.agents/tests/fixture.json'), '{}');
      assert.throws(() => api.runChecks([], { base: empty, spawn }), /No framework tests/);
      assert.throws(() => api.requiredCoverageFiles(empty), /ENOENT/);
      mkdirSync(join(empty, '.agents/lib'));
      writeFileSync(join(empty, '.agents/lib/README.md'), 'No code');
      assert.throws(() => api.requiredCoverageFiles(empty), /No framework library/);
    } finally { rmSync(empty, { recursive: true }); }
  } finally { process.exitCode = beforeExit; }
}

test('exported reporter and runner enforce measurements, invocation and error results', async () => {
  await exerciseChecker(checker, root);
});

test('runner uses Node coverage events and fails for missing production coverage and failing tests', () => {
  const temp = realpathSync(mkdtempSync(join(tmpdir(), 'caliber-framework-gate-')));
  try {
    for (const dir of ['bin', 'lib', 'tests']) mkdirSync(join(temp, '.agents', dir), { recursive: true });
    const checker = join(temp, '.agents/bin/check.mjs');
    copyFileSync(resolve(root, '.agents/bin/check.mjs'), checker);
    assert.throws(() => requiredCoverageFiles(temp), /No framework library/);
    writeFileSync(join(temp, '.agents/lib/sample.mjs'), 'export const value = 42;\n');
    writeFileSync(join(temp, '.agents/bin/engineering.mjs'), 'export const cli = 7;\n');
    const testPath = join(temp, '.agents/tests/sample.test.mjs');
    const fixture = "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\nimport { value } from '../lib/sample.mjs';\nimport { cli } from '../bin/engineering.mjs';\ntest('fixture', () => { assert.equal(value, 42); assert.equal(cli, 7); });\n";
    const checkerTests = `import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import * as checker from '../bin/check.mjs';
${exerciseChecker.toString()}
test('checker behavior', () => exerciseChecker(checker, ${JSON.stringify(temp)}));
`;
    writeFileSync(join(temp, '.agents/tests/checker.test.mjs'), checkerTests);
    writeFileSync(testPath, fixture);
    const env = { ...process.env };
    // A nested fixture is a separate test-runner process, not a child test file.
    delete env.NODE_TEST_CONTEXT;
    delete env.NODE_V8_COVERAGE;
    delete env.NODE_OPTIONS;
    const run = () => spawnSync(process.execPath, [checker], {
      cwd: tmpdir(), encoding: 'utf8', timeout: 30000,
      env,
    });
    let result = run();
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /Framework coverage gate: every production module and CLI exceeds/);
    writeFileSync(join(temp, '.agents/lib/unimported.mjs'), 'export const untouched = true;\n');
    result = run();
    assert.notEqual(result.status, 0);
    assert.match(result.stdout + result.stderr, /unimported\.mjs: missing measured coverage/);
    rmSync(join(temp, '.agents/lib/unimported.mjs'));
    writeFileSync(testPath, fixture.replace('assert.equal(value, 42)', 'assert.equal(value, -1)'));
    result = run();
    assert.notEqual(result.status, 0, 'a successful coverage report must not mask failing tests');
  } finally {
    rmSync(temp, { recursive: true });
  }
});
