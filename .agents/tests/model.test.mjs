import test from 'node:test';
import assert from 'node:assert/strict';
import { validateMapping, resolveTarget, analyzeScope } from '../lib/model.mjs';

// Deliberately unlike production: neither display names nor IDs are constants.
function fixture() {
  const config = {
    repository: 'team/product', project: { owner: 'team', number: 73 },
    fields: { status: 'Workflow', readiness: 'Can start', release: 'Ship', cycle: 'Iteration', priority: 'Rank', kind: 'Class', humanInput: 'Decision' },
    statuses: { todo: 'Queued', active: 'Building', done: 'Shipped' },
  };
  const fields = Object.entries(config.fields).map(([role, name], i) => ({
    id: `dynamic-${i}`, name, dataType: ['status', 'release'].includes(role) ? 'SINGLE_SELECT' : 'TEXT',
    ...(['status', 'release'].includes(role) ? { options: (role === 'status' ? Object.values(config.statuses) : ['Orchid', 'Future']).map((name, j) => ({ id: `${i}-${j}`, name })) } : {}),
  }));
  const milestone = { id: 'milestone-9', number: 9, title: 'Launch window', state: 'OPEN', description: 'Acceptance' };
  const snapshot = {
    capturedAt: '2026-11-04T00:00:00Z', repository: { nameWithOwner: config.repository, url: 'https://example.test/team/product', defaultBranch: 'trunk', headOid: 'abc' },
    project: { id: 'project-73', number: 73, title: 'Engineering', url: 'https://example.test/project/73', readme: '' },
    fields, milestones: [milestone], items: [],
  };
  function item(number, overrides = {}) {
    const value = {
      id: `item-${number}`, isArchived: false,
      fields: { Workflow: 'Queued', 'Can start': 'Ready', Ship: 'Orchid', Iteration: 'Sprint 8', Rank: 'P1', Class: 'Task', Decision: 'None' },
      issue: { id: `issue-${number}`, number, title: `Implement ${number}`, body: '', state: 'OPEN', url: `https://example.test/team/product/issues/${number}`, updatedAt: snapshot.capturedAt, repository: config.repository, labels: [], assignees: [], milestone, blockedBy: [], prs: [], subIssues: [], parent: null, ...overrides },
    };
    snapshot.items.push(value);
    return value;
  }
  const analyze = () => analyzeScope(snapshot, resolveTarget(snapshot, 'release:Orchid', config), config);
  return { config, snapshot, item, analyze };
}
const numbers = items => items.map(item => item.issue.number);
const codes = result => result.blockers.map(blocker => blocker.code);
const metadata = paths => `<!-- caliber-engineering-meta:${JSON.stringify({ owned_paths: paths, fingerprint: 'test' })} -->`;
function freeze(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
}

test('maps configured exact names to live objects and dynamic IDs', () => {
  const { snapshot, config } = fixture();
  const mapped = validateMapping(freeze(snapshot), freeze(config));
  assert.deepEqual(Object.keys(mapped), Object.keys(config.fields));
  assert.equal(mapped.priority.dataType, 'TEXT');
  assert.equal(mapped.status, snapshot.fields[0]);
  assert.equal(mapped.release.options[0].id, '2-0');
});

test('mapping fails explicitly for absent, ambiguous, reused or drifted fields', () => {
  const mutations = [
    [s => { s.repository.nameWithOwner = 'foreign/repo'; }, /repository/],
    [s => { delete s.fields; }, /fields/],
    [s => { s.fields[0].name = 'workflow'; }, /live field/],
    [s => { s.fields.push({ ...s.fields[0] }); }, /ambiguous/],
    [s => { s.fields[0].id = ''; }, /field ID/],
    [s => { s.fields[1].id = s.fields[0].id; }, /field ID/],
    [s => { s.fields[4].dataType = 'SINGLE_SELECT'; }, /type drift/],
    [s => { delete s.fields[0].options; }, /Missing options/],
    [s => { s.fields[0].options[0].id = ''; }, /options/],
    [s => { s.fields[0].options[0].name = ''; }, /options/],
    [s => { s.fields[0].options[1].id = s.fields[0].options[0].id; }, /options/],
    [s => { s.fields[0].options[1].name = s.fields[0].options[0].name; }, /options/],
    [s => { s.fields[0].options.pop(); }, /status option/],
  ];
  for (const [mutate, error] of mutations) {
    const { snapshot, config } = fixture(); mutate(snapshot);
    assert.throws(() => validateMapping(snapshot, config), error);
  }
  for (const mutate of [c => { delete c.fields; }, c => { c.fields.status = ''; }]) {
    const { snapshot, config } = fixture(); mutate(config);
    assert.throws(() => validateMapping(snapshot, config), /configured field/);
  }
  for (const mutate of [c => { delete c.statuses; }, c => { c.statuses.done = c.statuses.todo; }]) {
    const { snapshot, config } = fixture(); mutate(config);
    assert.throws(() => validateMapping(snapshot, config), /distinct/);
  }
  const { snapshot, config } = fixture();
  delete config.statuses.done;
  assert.throws(() => validateMapping(snapshot, config), /status option/);
  assert.throws(() => validateMapping(snapshot, null), /repository/);
});

test('resolves case-insensitive exact live names across all three target domains', () => {
  const { snapshot, config, item } = fixture();
  item(1); item(2, { milestone: null });
  for (const [argument, kind, key] of [
    ['oRcHiD', 'release', 'release:Orchid'], [' RELEASE:orchid ', 'release', 'release:Orchid'],
    ['sprint 8', 'cycle', 'cycle:Sprint 8'], ['Cycle:SPRINT 8', 'cycle', 'cycle:Sprint 8'],
    ['launch WINDOW', 'milestone', 'milestone:#9'], ['milestone:#9', 'milestone', 'milestone:#9'],
  ]) {
    const target = resolveTarget(snapshot, argument, config);
    assert.equal(target.kind, kind); assert.equal(target.key, key);
    assert.deepEqual(numbers(target.items), kind === 'milestone' ? [1] : [1, 2]);
    assert.deepEqual(target.milestones, snapshot.milestones);
  }
});

test('ignores archived, foreign-repository and non-issue project items', () => {
  const { snapshot, config, item } = fixture();
  item(1); item(2).isArchived = true; item(3, { repository: 'other/product' });
  snapshot.items.push({ id: 'draft', fields: {}, issue: null });
  assert.deepEqual(numbers(resolveTarget(snapshot, 'Orchid', config).items), [1]);
  snapshot.items[0].isArchived = true;
  assert.throws(() => resolveTarget(snapshot, 'Orchid', config), /Empty live/);
  assert.throws(() => resolveTarget(snapshot, 'cycle:Sprint 8', config), /Unknown live/);
});

test('hidden in-release or unknown-scope items block acceptance without suppressing independent development', () => {
  const { snapshot, config, item } = fixture();
  item(1);
  const hidden = [
    { id: 'draft', contentType: 'DraftIssue', fields: { Ship: 'Orchid' } },
    { id: 'redacted', contentType: 'Redacted', fields: { Ship: 'ORCHID' } },
    { id: 'unknown-release', contentType: 'Redacted', fields: {} },
    { id: 'empty-release', contentType: 'DraftIssue', fields: { Ship: '' } },
    { id: 'unknown-fields', contentType: 'Redacted' },
  ].map(value => ({ ...value, isArchived: false, issue: null }));
  snapshot.items.push(...hidden,
    { id: 'other-release', issue: null, contentType: 'Redacted', fields: { Ship: 'Future' } },
    { id: 'archived-hidden', issue: null, isArchived: true, fields: { Ship: 'Orchid' } });
  item(2, { repository: 'other/repo' });
  const before = structuredClone(snapshot);
  const target = resolveTarget(freeze(snapshot), 'release:Orchid', freeze(config));
  const result = analyzeScope(snapshot, freeze(target), config);
  assert.deepEqual(result.uninspectableItems, hidden);
  assert.deepEqual(result.blockers.filter(b => b.code === 'scope-uninspectable').map(b => b.item), hidden);
  assert.deepEqual(numbers(result.readyTasks), [1]);
  assert.deepEqual(numbers(result.openItems), [1]);
  assert.deepEqual(result.parallelBatches.map(numbers), [[1]]);
  assert.deepEqual(snapshot, before);
});

test('milestone and cycle targets retain hidden items whose scope cannot be established', () => {
  const { snapshot, config, item } = fixture();
  item(1);
  const hidden = { id: 'unknown-milestone', isArchived: false, issue: null, contentType: 'Redacted', fields: { Ship: 'Future' } };
  snapshot.items.push(hidden);
  for (const argument of ['milestone:#9', 'cycle:Sprint 8']) {
    const result = analyzeScope(snapshot, resolveTarget(snapshot, argument, config), config);
    assert.deepEqual(result.uninspectableItems, [hidden]);
    assert.ok(codes(result).includes('scope-uninspectable'));
    assert.deepEqual(numbers(result.readyTasks), [1]);
  }
});

test('rejects empty, unknown, partial and ambiguous targets without guessing', () => {
  const { snapshot, config, item } = fixture(); item(1);
  for (const argument of ['', '  ', null, 'cycle: ', 'release:']) assert.throws(() => resolveTarget(snapshot, argument, config), /empty/);
  for (const argument of ['Orch', 'beta', 'milestone:#999', 'foo:Orchid']) assert.throws(() => resolveTarget(snapshot, argument, config), /Unknown/);
  assert.throws(() => resolveTarget(snapshot, 'Future', config), /Empty/);
  snapshot.items[0].fields.Iteration = 'Orchid';
  assert.throws(() => resolveTarget(snapshot, 'Orchid', config), /Ambiguous/);
  assert.equal(resolveTarget(snapshot, 'cycle:Orchid', config).kind, 'cycle');
  snapshot.milestones.push({ ...snapshot.milestones[0], id: 'second', number: 10 });
  assert.throws(() => resolveTarget(snapshot, 'milestone:Launch window', config), /Ambiguous/);
  assert.equal(resolveTarget(snapshot, 'milestone:#9', config).key, 'milestone:#9');
  snapshot.fields[2].options.push({ id: 'case-collision', name: 'ORCHID' });
  assert.throws(() => resolveTarget(snapshot, 'release:Orchid', config), /Ambiguous/);
});

test('only explicit tasks enter eligibility; controls and coordination remain distinct', () => {
  const { item, analyze } = fixture();
  item(1); item(2, { labels: ['TASK'] }).fields.Class = '';
  item(3).fields.Class = 'Epic';
  item(4, { labels: ['task', 'epic'] });
  item(5, { labels: ['milestone-control', 'task'] });
  item(6, { title: '[D8] Milestone control: acceptance' });
  item(7).fields.Class = 'Control';
  item(8, { labels: ['coordination', 'task'] });
  item(9).fields.Class = 'Coordination';
  item(10, { labels: ['human-input', 'task'] });
  item(11).fields.Class = 'Unclassified';
  const result = analyze();
  assert.deepEqual(numbers(result.readyTasks), [1, 2]);
  assert.equal(result.openItems.length, 11);
  assert.equal(result.blockers.filter(b => b.code === 'acceptance-evidence-required').length, 3);
  assert.equal(result.blockers.filter(b => b.code === 'not-actionable-task').length, 6);
});

test('claim detection survives readiness drift and excludes assignees, active status and open PRs', () => {
  const { item, analyze } = fixture();
  item(1, { assignees: ['owner'] }).fields['Can start'] = 'Blocked';
  item(2).fields.Workflow = 'Building';
  item(3, { prs: [{ number: 20, state: 'OPEN', mergedAt: null }] });
  item(4, { prs: [{ number: 21, state: 'MERGED', mergedAt: '2026-11-03' }, { number: 22, state: 'CLOSED', mergedAt: null }] });
  item(5).fields.Workflow = 'Shipped';
  item(6).fields.Workflow = undefined;
  item(7, { state: 'UNKNOWN' });
  item(8, { state: 'CLOSED', assignees: ['owner'] });
  const result = analyze();
  assert.deepEqual(numbers(result.claimedTasks), [1, 2, 3]);
  assert.deepEqual(numbers(result.readyTasks), [4]);
  assert.equal(result.openItems.length, 7);
  assert.ok(codes(result).includes('unknown-state'));
  assert.ok(codes(result).includes('not-ready'));
  assert.ok(codes(result).includes('status-not-todo'));
});

test('Gate kind is an acceptance control even without a label and overrides task labels', () => {
  const { item, analyze } = fixture();
  item(1).fields.Class = 'Gate';
  item(2, { labels: ['task'], state: 'CLOSED' }).fields.Class = 'GATE';
  const result = analyze();
  assert.deepEqual(result.readyTasks, []);
  assert.deepEqual(codes(result), ['acceptance-evidence-required', 'acceptance-evidence-required']);
});

test('priority and issue number determine ready order without mutating project order', () => {
  const { snapshot, item, analyze } = fixture();
  for (const [number, priority] of [[40, 'P3'], [20, 'P1'], [9, 'P0'], [3, 'P1'], [6, 'P2'], [2, 'P0']]) {
    item(number).fields.Rank = priority;
  }
  const before = numbers(snapshot.items);
  const result = analyze();
  assert.deepEqual(numbers(result.readyTasks), [2, 9, 3, 20, 6, 40]);
  assert.deepEqual(result.parallelBatches.map(numbers), [[2], [9], [3], [20], [6], [40]]);
  assert.deepEqual(numbers(snapshot.items), before);
});

test('missing or unknown priorities require planner classification; blank cycles do not block readiness', () => {
  const { item, analyze } = fixture();
  for (const [index, priority] of [undefined, '', 'P4', 'Urgent', 'p1'].entries()) {
    item(index + 1).fields.Rank = priority;
  }
  item(10).fields.Iteration = '';
  delete item(11).fields.Iteration;
  const result = analyze();
  assert.deepEqual(numbers(result.readyTasks), [10, 11]);
  assert.equal(result.blockers.filter(b => b.code === 'priority-unclassified').length, 5);
});

test('human input is fail-closed for prose, including misleading None required prefixes', () => {
  const { item, analyze } = fixture();
  const safe = ['', undefined, 'None', 'NO', 'n/a', ' Not required ', 'None pending'];
  const unsafe = ['None required until owner approval', 'None required...', 'Resolved H1; H2 pending', 'Coordination', 'Acceptance: H03', 'Start blocked', 'H04: start', 'H02: child phase,H03: child phase', 'H01: acceptance, H04: start', 'H01: acceptance pending', 'H01: acceptance,', 'None pending except approval'];
  [...safe, ...unsafe].forEach((value, index) => { item(index + 1).fields.Decision = value; });
  const result = analyze();
  assert.deepEqual(numbers(result.readyTasks), [1, 2, 3, 4, 5, 6, 7]);
  assert.equal(result.blockers.filter(b => b.code === 'human-input').length, unsafe.length);
});

test('exact acceptance-only human inputs permit development but preserve acceptance blockers', () => {
  const { item, analyze } = fixture();
  item(1).fields.Decision = 'H01: acceptance';
  item(2).fields.Decision = 'H01: acceptance, H09: acceptance';
  const result = analyze();
  assert.deepEqual(numbers(result.readyTasks), [1, 2]);
  assert.deepEqual(result.blockers.filter(b => b.code === 'acceptance-human-input').map(b => b.item.issue.number), [1, 2]);
});

test('bug/review/critique metadata categories do not override the explicit Task contract', () => {
  const { item, analyze } = fixture();
  ['Bug', 'Review', 'Critique'].forEach((category, i) => item(i + 1, {
    body: `<!-- caliber-engineering-meta:${JSON.stringify({ category })} -->`,
  }));
  assert.deepEqual(numbers(analyze().readyTasks), [1, 2, 3]);
});

test('native dependencies use repository-qualified identity and explicit closed states', () => {
  const { item, analyze } = fixture();
  item(1, { blockedBy: [{ number: 99, repository: 'external/repo', state: 'OPEN' }] });
  item(2, { blockedBy: [{ number: 99, repository: 'external/repo', state: 'CLOSED', stateReason: 'COMPLETED' }] });
  item(3, { blockedBy: [{ number: 404, repository: 'team/product' }] });
  item(4, { blockedBy: [{ number: 99, repository: 'team/product', state: 'CLOSED', stateReason: 'COMPLETED' }] });
  item(5, { blockedBy: [{ number: 98, repository: 'team/product', state: 'CLOSED', stateReason: 'COMPLETED' }] });
  item(98, { state: 'OPEN' });
  item(99, { state: 'CLOSED', stateReason: 'COMPLETED' });
  const result = analyze();
  assert.deepEqual(numbers(result.readyTasks), [2, 4, 98]);
  assert.deepEqual(result.blockers.filter(b => b.code === 'native-blocker').map(b => b.item.issue.number), [1, 3, 5]);
});

test('cycles, self-dependencies and downstream tasks never become eligible, even with closed edges', () => {
  const { item, analyze } = fixture();
  const dep = number => ({ number, repository: 'team/product', state: 'CLOSED', stateReason: 'COMPLETED' });
  item(1, { blockedBy: [dep(2)] });
  item(2, { blockedBy: [dep(1)] });
  item(3, { blockedBy: [dep(1)] });
  item(4, { blockedBy: [dep(4)] });
  item(5, { blockedBy: [dep(6), dep(7)] });
  item(6, { state: 'CLOSED', stateReason: 'COMPLETED', blockedBy: [dep(8)] });
  item(7, { state: 'CLOSED', stateReason: 'COMPLETED', blockedBy: [dep(8)] });
  item(8, { state: 'CLOSED', stateReason: 'COMPLETED' });
  const result = analyze();
  assert.deepEqual(numbers(result.readyTasks), [5]);
  assert.deepEqual(result.blockers.filter(b => b.code === 'dependency-cycle').map(b => b.item.issue.number), [1, 2, 3, 4]);
});

test('only CLOSED plus COMPLETED satisfies native blockers, including untracked and cross-repository dependencies', () => {
  for (const repository of ['team/product', 'outside/repo']) {
    for (const [state, stateReason, eligible] of [
      ['CLOSED', 'COMPLETED', true], ['CLOSED', 'NOT_PLANNED', false],
      ['CLOSED', undefined, false], ['CLOSED', null, false], ['CLOSED', 'OTHER', false],
      ['OPEN', 'COMPLETED', false], [undefined, 'COMPLETED', false],
    ]) {
      const { item, analyze } = fixture();
      item(1, { blockedBy: [{ number: 99, repository, state, stateReason }] });
      const result = analyze();
      assert.deepEqual(numbers(result.readyTasks), eligible ? [1] : [], `${repository} ${state}/${stateReason}`);
      assert.equal(codes(result).includes('native-blocker'), !eligible);
    }
  }
});

test('tracked state disagreement blocks selection and never reopens abandoned or unclassified closed work', () => {
  for (const stateReason of ['NOT_PLANNED', undefined, null]) {
    const { item, analyze } = fixture();
    item(1, { blockedBy: [{ number: 99, repository: 'team/product', state: 'CLOSED', stateReason: 'COMPLETED' }] });
    item(99, { state: 'CLOSED', stateReason });
    const result = analyze();
    assert.deepEqual(numbers(result.openItems), [1]);
    assert.deepEqual(result.readyTasks, []);
    assert.ok(codes(result).includes('native-blocker'));
  }
  const { item, analyze } = fixture();
  item(1, { blockedBy: [{ number: 99, repository: 'team/product', state: 'CLOSED' }] });
  item(99, { state: 'CLOSED', stateReason: 'COMPLETED' });
  assert.deepEqual(analyze().readyTasks, [], 'a tracked completion must not override an incomplete native relation');
});

test('closed issues and controls do not claim release acceptance', () => {
  const { item, analyze } = fixture();
  item(1, { state: 'CLOSED' });
  let result = analyze();
  assert.deepEqual(result.openItems, []); assert.deepEqual(result.readyTasks, []);
  assert.deepEqual(codes(result), ['acceptance-control-missing']);
  assert.equal(result.blockers[0].item, null);
  item(2, { state: 'CLOSED', labels: ['milestone-control'] });
  result = analyze();
  assert.deepEqual(codes(result), ['acceptance-evidence-required']);
});

test('parallel batches require explicit disjoint ownership, cap four and preserve every ready task', () => {
  const { item, analyze } = fixture();
  for (let n = 1; n <= 6; n++) item(n, { body: metadata([`component-${n}/**`]) });
  item(7); item(8);
  const result = analyze();
  assert.deepEqual(result.parallelBatches.map(numbers), [[1, 2, 3, 4], [5, 6], [7], [8]]);
});

test('overlapping directories, files and glob prefixes never share a batch', () => {
  for (const [left, right] of [
    ['src', 'src/module/file.mjs'], ['src/file.mjs', 'src'], ['src/**', 'src/module/**'],
    ['src/*.mjs', 'src/file.mjs'], ['src/file.mjs', 'src/file.mjs'],
    ['src/a?.mjs', 'src/b.mjs'], ['**/*.mjs', 'docs/guide.md'],
    ['src/{a,b}/**', 'src/c/file'], ['src/[abc]/**', 'src/c/file'],
    ['SRC/module', 'src/module/file'],
  ]) {
    const { item, analyze } = fixture();
    item(1, { body: metadata([left]) }); item(2, { body: metadata([right]) });
    assert.deepEqual(analyze().parallelBatches.map(numbers), [[1], [2]], `${left} overlaps ${right}`);
  }
  const { item, analyze } = fixture();
  item(1, { body: metadata(['src/a.mjs', 'docs/']) });
  item(2, { body: metadata(['src/b.mjs']) });
  item(3, { body: metadata(['docs/guide.md']) });
  assert.deepEqual(analyze().parallelBatches.map(numbers), [[1, 2], [3]]);
});

test('malformed, ambiguous and unsafe ownership metadata serializes without trusting lanes', () => {
  for (const body of [
    '', '<!-- caliber-engineering-meta:bad-json -->', '<!-- caliber-engineering-meta:null -->',
    '<!-- caliber-engineering-meta:{} -->', metadata([]), metadata('src'),
    metadata(['']), metadata([null]), metadata([' /tmp ']), metadata(['/tmp']), metadata(['../outside']),
    metadata(['src/./a']), metadata(['src//a']), metadata(['src\\file']), metadata(['C:/file']), metadata(['src/\u0000a']),
    `${metadata(['src/a'])}\n${metadata(['src/b'])}`,
  ]) {
    const { item, analyze } = fixture();
    item(1, { body }); item(2, { body: metadata(['unrelated/file']) });
    assert.deepEqual(analyze().parallelBatches.map(numbers), [[1], [2]], body);
  }
});

test('all operations leave snapshots, config and targets unchanged and reject empty live scope', () => {
  const { snapshot, config, item } = fixture();
  item(1, { body: metadata(['one/**']) }); item(2, { body: metadata(['two/**']) });
  const before = structuredClone(snapshot);
  const target = resolveTarget(freeze(snapshot), 'Orchid', freeze(config));
  analyzeScope(snapshot, freeze(target), config);
  assert.deepEqual(snapshot, before);
  assert.throws(() => analyzeScope(snapshot, { items: [] }, config), /empty live scope/);
});
