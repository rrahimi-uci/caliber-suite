import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, chmodSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { parse, run, lock, ROOT, USAGE } from '../lib/cli.mjs';

function fixture() {
  const config = { repository: 'test/repo', project: { owner: 'test', number: 1 }, fields: { status: 'Status', readiness: 'Readiness', release: 'Release', cycle: 'Cycle', priority: 'Priority', kind: 'Kind', humanInput: 'Input' }, statuses: { todo: 'Todo', active: 'Working', done: 'Done' } };
  const milestone = { number: 1, id: 'm1', title: 'Window', state: 'OPEN' };
  const snapshot = { capturedAt: 'now', repository: { nameWithOwner: 'test/repo', defaultBranch: 'trunk' }, project: { id: 'p1' }, fields: Object.entries(config.fields).map(([role, name]) => ({ id: name, name, dataType: ['status', 'release'].includes(role) ? 'SINGLE_SELECT' : 'TEXT', options: (role === 'status' ? ['Todo', 'Working', 'Done'] : ['Spring']).map(name => ({ name, id: name })) })), milestones: [milestone], items: [{ id: 'i1', fields: { Status: 'Todo', Readiness: 'Ready', Release: 'Spring', Cycle: 'W1', Kind: 'Task', Input: 'None' }, issue: { id: 'n1', number: 1, title: 'Existing', body: '', state: 'OPEN', repository: 'test/repo', milestone, labels: [], assignees: [], prs: [], blockedBy: [], updatedAt: 'now' } }] };
  const payload = { title: 'New work', kind: 'Task', objective: 'Outcome', context: 'Evidence', acceptance: ['Observable'], tests: ['Regression'], components: ['lib/a'], complexity: 'Small', parallelization: 'Serial', priority: 'P2', dependencies: [] };
  const events = [];
  const deps = { config, github: { async snapshot() { events.push('project'); return snapshot; }, allIssues() { events.push('issues'); return []; }, issue() { return { updated_at: 'now', state: 'open', assignees: [] }; }, comment() { events.push('comment'); }, setField() { events.push('field'); } }, read() { events.push('payload'); return payload; }, lock() { events.push('lock'); return () => events.push('unlock'); } };
  return { config, snapshot, payload, events, deps };
}

test('parser requires exact arity and rejects unsupported/duplicate options', () => {
  for (const args of [[], ['--help'], ['-h'], ['help']]) assert.deepEqual(parse(args), { help: true });
  assert.deepEqual(parse(['transition', 'Cycle:X', '7', 'claim', '--file', 'claim.json', '--apply']), { operation: 'transition', rest: ['Cycle:X', '7', 'claim'], file: 'claim.json', apply: true });
  for (const args of [['bogus'], ['schema', 'x'], ['context', 'plan'], ['issue', 'Spring'], ['schema', '--apply'], ['context', 'plan', 'Spring', '--file', 'x'], ['schema', '--wat'], ['issue', 'x', '--file'], ['issue', 'x', '--file', '--apply'], ['issue', 'x', '--file', 'a', '--file', 'b'], ['issue', 'x', '--file', 'a', '--apply', '--apply'], ['transition', 'x', '-1', 'done'], ['transition', 'x', '0', 'done']]) assert.throws(() => parse(args));
});

test('help does not access credentials or project', async () => {
  assert.equal(await run([]), USAGE);
});

test('schema and all seven context commands read project before task processing', async () => {
  const { deps, snapshot, events } = fixture();
  assert.deepEqual((await run(['schema'], deps)).fields, snapshot.fields);
  for (const command of ['architect', 'plan', 'develop', 'review', 'critique', 'bug-bash', 'release-check']) {
    const result = await run(['context', command, 'Spring'], deps);
    assert.equal(result.target.items.length, 1);
    assert.ok(result.instructions.command.endsWith(`/${command}.md`));
    assert.match(result.notice, /not an executed/);
  }
  assert.deepEqual(events, Array(8).fill('project'));
});

test('issue dry-run reads live project before JSON payload, never locks or writes', async () => {
  const { deps, events } = fixture();
  const result = await run(['issue', 'Spring', '--file', 'x.json'], deps);
  assert.equal(result.applied, false);
  assert.deepEqual(events, ['project', 'payload', 'issues']);
});

test('transition dry-run has no writes and apply always releases coordinator lock', async () => {
  const { deps, events } = fixture();
  const dry = await run(['transition', 'Spring', '1', 'planned'], deps);
  assert.equal(dry.applied, false);
  events.length = 0;
  const result = await run(['transition', 'Spring', '1', 'planned', '--apply'], deps);
  assert.equal(result.applied, true);
  assert.deepEqual(events, ['lock', 'project', 'project', 'comment', 'field', 'field', 'unlock']);
});

test('invalid payloads and project failures cannot write and cannot leak locks', async () => {
  for (const payload of [null, [], 'bad']) {
    const { deps, events } = fixture(); deps.read = () => payload;
    await assert.rejects(run(['issue', 'Spring', '--file', 'x', '--apply'], deps), /JSON object/);
    assert.deepEqual(events, ['lock', 'project', 'unlock']);
  }
  const { deps, events } = fixture();
  deps.github.snapshot = async () => { throw new Error('permission denied'); };
  await assert.rejects(run(['issue', 'Spring', '--file', 'x', '--apply'], deps), /permission denied/);
  assert.deepEqual(events, ['lock', 'unlock']);
});

test('default config and JSON file reading resolve repository root independently of cwd', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'caliber-json-'));
  try {
    const { deps } = fixture();
    const file = join(folder, 'payload.json'); writeFileSync(file, '{bad');
    delete deps.read;
    await assert.rejects(run(['issue', 'Spring', '--file', file], deps), SyntaxError);
    delete deps.config;
    await assert.rejects(run(['schema'], deps), /repository does not match/);
  } finally { rmSync(folder, { recursive: true }); }
});

test('real lock serializes sibling worktrees and refuses stale locks without deleting them', () => {
  const folder = mkdtempSync(join(tmpdir(), 'caliber-lock-'));
  const repo = join(folder, 'repo'), sibling = join(folder, 'sibling');
  mkdirSync(repo);
  try {
    execFileSync('git', ['init', '-q'], { cwd: repo });
    execFileSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'Initial'], { cwd: repo });
    execFileSync('git', ['worktree', 'add', '-qb', 'sibling', sibling], { cwd: repo });
    const release = lock(repo);
    assert.throws(() => lock(sibling), /Another local coordinator/);
    assert.throws(() => lock(repo), /Another local coordinator/);
    release();
    lock(sibling)();
    writeFileSync(join(repo, '.git', 'caliber-engineering-write.lock'), 'stale');
    assert.throws(() => lock(repo), /Another local coordinator/);
    rmSync(join(repo, '.git', 'caliber-engineering-write.lock'));
    assert.throws(() => lock(join(folder, 'missing')));
  } finally { rmSync(folder, { recursive: true }); }
});

test('executable wrapper returns help and errors with distinct exit status without network', () => {
  const bin = join(ROOT, '.agents/bin/engineering.mjs');
  const help = spawnSync(process.execPath, [bin, '--help'], { encoding: 'utf8' });
  assert.equal(help.status, 0); assert.match(help.stdout, /Codex or Claude/); assert.equal(help.stderr, '');
  const invalid = spawnSync(process.execPath, [bin, 'unknown'], { encoding: 'utf8' });
  assert.equal(invalid.status, 1); assert.match(invalid.stderr, /Repository engineering helper/); assert.equal(invalid.stdout, '');
});


test('executable runs schema through a real subprocess adapter with a fake gh, without network', () => {
  const folder = mkdtempSync(join(tmpdir(), 'caliber-fake-gh-'));
  try {
    const config = JSON.parse(readFileSync(join(ROOT, '.agents/github/project.json'), 'utf8'));
    const { snapshot } = fixture();
    snapshot.fields = Object.entries(config.fields).map(([role, name]) => ({ id: name, name, dataType: ['status', 'release'].includes(role) ? 'SINGLE_SELECT' : 'TEXT', options: (role === 'status' ? Object.values(config.statuses) : ['Spring']).map(name => ({ id: name, name })) }));
    const script = `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
let result;
if(args[0]==='project') result={id:'live-project',number:3};
else if(args.includes('--slurp')) result=[[]];
else {
 const {query}=JSON.parse(fs.readFileSync(0,'utf8'));
 if(query.includes('fields(first:')) result={data:{node:{fields:{nodes:${JSON.stringify(snapshot.fields)},pageInfo:{hasNextPage:false}}}}};
 else if(query.includes('defaultBranchRef')) result={data:{repository:{nameWithOwner:${JSON.stringify(config.repository)},defaultBranchRef:{name:'main',target:{oid:'abc'}}}}};
 else result={data:{node:{items:{nodes:[],pageInfo:{hasNextPage:false}}}}};
}
process.stdout.write(JSON.stringify(result));
`;
    const gh = join(folder, 'gh'); writeFileSync(gh, script); chmodSync(gh, 0o755);
    const result = spawnSync(process.execPath, [join(ROOT, '.agents/bin/engineering.mjs'), 'schema'], { cwd: folder, encoding: 'utf8', env: { ...process.env, PATH: `${folder}:${process.env.PATH}` } });
    assert.equal(result.status, 0, result.stderr);
    const output = JSON.parse(result.stdout);
    assert.equal(output.project.id, 'live-project');
    assert.equal(output.mapping.repository, config.repository);
    assert.equal(output.fields.length, 7);
  } finally { rmSync(folder, { recursive: true }); }
});
