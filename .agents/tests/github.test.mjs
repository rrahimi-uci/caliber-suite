import test from 'node:test';
import assert from 'node:assert/strict';
import { GitHub, ensureComplete, values } from '../lib/github.mjs';

const config = { repository: 'acme/engine', project: { owner: 'acme', number: 17 } };
const connection = (nodes, hasNextPage = false, endCursor = null) => ({ nodes, pageInfo: { hasNextPage, endCursor } });
const field = (name, value) => ({ field: { name }, ...value });
const issue = () => ({
  __typename: 'Issue', id: 'issue-5', number: 5, title: 'Implement contract', body: 'Acceptance criteria',
  state: 'OPEN', url: 'https://example.test/5', updatedAt: '2026-11-04T00:00:00Z',
  repository: { nameWithOwner: 'acme/engine' }, milestone: { id: 'mile-2', number: 2, title: 'Release window', state: 'OPEN', description: 'Goal' },
  parent: { number: 1, url: 'https://example.test/1' },
  labels: connection([{ name: 'task' }]), assignees: connection([{ login: 'worker' }]),
  blockedBy: connection([{ id: 'external-4', number: 4, repository: { nameWithOwner: 'other/repo' }, state: 'CLOSED', stateReason: 'COMPLETED', url: 'https://example.test/external/4' }]),
  closedByPullRequestsReferences: connection([{ number: 10, url: 'https://example.test/pr/10', state: 'OPEN', mergedAt: null, headRefOid: 'head-10' }]),
  subIssues: connection([{ number: 6, state: 'OPEN', url: 'https://example.test/6' }]),
});

// Exercise the public adapter through an injected execFileSync replacement.
// Every subprocess command must match a fixture; accidental live calls fail.
function harness(change = () => {}) {
  const state = {
    project: { id: 'proj-17', number: 17, title: 'Engineering', url: 'https://example.test/projects/17', readme: 'Policy', closed: false, owner: { login: 'acme' } },
    fields: [connection([{ id: 'status-8', name: 'Status', dataType: 'SINGLE_SELECT', options: [{ id: 'todo-3', name: 'Todo' }] }], true, 'field-page-2'), connection([{ id: 'cycle-9', name: 'Cycle', dataType: 'TEXT' }])],
    repository: { nameWithOwner: 'acme/engine', url: 'https://example.test/acme/engine', defaultBranchRef: { name: 'trunk', target: { oid: 'head-abc' } } },
    milestones: [[{ node_id: 'mile-2', number: 2, title: 'Release window', state: 'open', description: 'Goal' }], [{ node_id: 'mile-3', number: 3, title: 'Next window', state: 'closed', description: null }]],
    items: [connection([{ id: 'item-5', isArchived: false, fieldValues: connection([field('Status', { name: 'Todo' }), field('Cycle', { text: 'Wave 8' })]), content: issue() }], true, 'item-page-2'), connection([
      { id: 'draft-6', isArchived: false, fieldValues: connection([]), content: { __typename: 'DraftIssue' } },
      { id: 'redacted-7', isArchived: true, fieldValues: connection([]), content: null },
    ])],
  };
  change(state);
  const calls = [];
  let fieldPage = 0, itemPage = 0;
  const adapter = new GitHub((command, args, options) => {
    assert.equal(command, 'gh');
    const payload = options.input === undefined ? undefined : JSON.parse(options.input);
    calls.push({ args, payload, options });
    if (args.includes('--slurp') && args.at(-1).includes('/milestones?')) return JSON.stringify(state.milestones);
    assert.deepEqual(args, ['api', 'graphql', '--input', '-']);
    if (payload.query.includes('projectV2(number:')) {
      assert.deepEqual(payload.variables, { login: 'acme', number: 17 });
      assert.ok(!payload.query.includes('items('), 'initial Project lookup must not overfetch items');
      return JSON.stringify({ data: { user: { projectV2: state.project } } });
    }
    if (payload.query.includes('fields(first:')) {
      assert.deepEqual(payload.variables, { id: 'proj-17', after: fieldPage ? 'field-page-2' : null });
      return JSON.stringify({ data: { node: { fields: state.fields[fieldPage++] } } });
    }
    if (payload.query.includes('defaultBranchRef')) {
      assert.deepEqual(payload.variables, { owner: 'acme', name: 'engine' });
      return JSON.stringify({ data: { repository: state.repository } });
    }
    assert.match(payload.query, /items\(first:/);
    assert.match(payload.query, /blockedBy\([^)]*\)\{nodes\{[^{}]*\bstateReason\b/,
      'native dependency completion must be requested from GitHub, not inferred from CLOSED');
    assert.deepEqual(payload.variables, { id: 'proj-17', after: itemPage ? 'item-page-2' : null });
    return JSON.stringify({ data: { node: { items: state.items[itemPage++] } } });
  });
  return { adapter, calls, state };
}

test('gh uses argument arrays, serialized stdin and bounded subprocess resources', () => {
  const calls = [];
  const adapter = new GitHub((command, args, options) => { calls.push({ command, args, options }); return '{"ok":true}'; });
  const body = 'literal $(do-not-run) `shell`\nprivate-body';
  assert.deepEqual(adapter.gh(['api', 'test'], { body }), { ok: true });
  assert.deepEqual(calls[0], { command: 'gh', args: ['api', 'test'], options: {
    encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, input: JSON.stringify({ body }), stdio: ['pipe', 'pipe', 'pipe'], timeout: 120000,
  } });
  assert.equal(new GitHub(() => '  \n').gh(['api']), null);
  assert.throws(() => new GitHub(() => 'invalid JSON').gh(['api', 'test']), /GitHub command failed/);
  assert.throws(() => new GitHub(() => { throw new Error('executable unavailable'); }).gh(['api', 'test']), /GitHub command failed/);
  assert.throws(() => new GitHub(() => { throw Object.assign(new Error('private diagnostic'), { status: 4 }); }).gh(['api', 'test']), /exit 4/);
  assert.throws(() => new GitHub(() => { throw Object.assign(new Error('failed'), { stderr: 'x'.repeat(3000) }); }).gh(['api', 'test']), error => error.message.length < 1600);
});

test('command errors must not disclose submitted issue content or credentials echoed by gh', () => {
  const secret = 'ghp_sensitive_test_fixture';
  const body = 'private roadmap text';
  const adapter = new GitHub(() => { throw Object.assign(new Error(body), { stderr: `bad response ${secret} ${body}` }); });
  assert.throws(() => adapter.createIssue(config.repository, { body }), error => {
    assert.match(error.message, /GitHub command failed/);
    assert.ok(!error.message.includes(secret)); assert.ok(!error.message.includes(body));
    return true;
  });
});

test('graphql forwards variables and rejects server errors or missing data', () => {
  const adapter = new GitHub((command, args, options) => {
    assert.deepEqual(args, ['api', 'graphql', '--input', '-']);
    assert.deepEqual(JSON.parse(options.input), { query: 'query Fixture', variables: {} });
    return '{"data":{"node":{"id":"live"}}}';
  });
  assert.deepEqual(adapter.graphql('query Fixture'), { node: { id: 'live' } });
  assert.throws(() => new GitHub(() => '{"errors":[{"message":"denied"},{"message":"missing scope"}]}').graphql('query'), /denied; missing scope/);
  assert.throws(() => new GitHub(() => '{"errors":[],"data":null}').graphql('query'), /no data/);
});

test('snapshot reads project first, follows field/item cursors and normalizes live data', async () => {
  const { adapter, calls } = harness();
  const snapshot = await adapter.snapshot(config);
  assert.deepEqual(calls[0].args, ['api', 'graphql', '--input', '-']);
  assert.equal(calls.length, 7);
  assert.ok(Number.isFinite(Date.parse(snapshot.capturedAt)));
  assert.deepEqual(snapshot.repository, { nameWithOwner: 'acme/engine', url: 'https://example.test/acme/engine', defaultBranch: 'trunk', headOid: 'head-abc' });
  assert.equal(snapshot.project.id, 'proj-17');
  assert.deepEqual(snapshot.fields.map(x => x.id), ['status-8', 'cycle-9']);
  assert.deepEqual(snapshot.milestones.map(x => [x.id, x.state, x.description]), [['mile-2', 'OPEN', 'Goal'], ['mile-3', 'CLOSED', '']]);
  assert.deepEqual(snapshot.items[0].fields, { Status: 'Todo', Cycle: 'Wave 8' });
  const value = snapshot.items[0].issue;
  assert.equal(value.repository, 'acme/engine'); assert.deepEqual(value.labels, ['task']); assert.deepEqual(value.assignees, ['worker']);
  assert.equal(value.blockedBy[0].repository, 'other/repo'); assert.equal(value.blockedBy[0].state, 'CLOSED');
  assert.equal(value.blockedBy[0].stateReason, 'COMPLETED');
  assert.equal(value.prs[0].headRefOid, 'head-10'); assert.equal(value.prs[0].state, 'OPEN');
  assert.equal(value.subIssues[0].number, 6); assert.equal(value.parent.number, 1);
  assert.equal(snapshot.items[1].issue, null); assert.equal(snapshot.items[1].contentType, 'DraftIssue');
  assert.equal(snapshot.items[2].issue, null); assert.equal(snapshot.items[2].contentType, 'Redacted');
  assert.equal(snapshot.items[2].isArchived, true);
});

test('missing, closed, or mismatched Project halts before reading repository data', async () => {
  for (const project of [null, {}, { id: 'proj-17', closed: true }, { id: 'proj-17', closed: false, owner: { login: 'other' } }]) {
    const { adapter, calls } = harness(s => { s.project = project; });
    await assert.rejects(adapter.snapshot(config), /Project is unavailable, closed, or owned by another account/);
    assert.equal(calls.length, 1);
  }
  const calls = [];
  const adapter = new GitHub((command, args) => { calls.push(args); throw new Error('denied'); });
  await assert.rejects(adapter.snapshot(config), /GitHub command failed/);
  assert.deepEqual(calls.map(args => args.slice(0, 2)), [['api', 'graphql']]);
});

test('missing repository or default branch cannot produce a snapshot', async () => {
  for (const repository of [null, { nameWithOwner: 'acme/engine', defaultBranchRef: null }]) {
    const { adapter, calls } = harness(s => { s.repository = repository; });
    await assert.rejects(adapter.snapshot(config), /Repository\/default branch/);
    assert.equal(calls.length, 4);
  }
});

test('pagination without a next cursor fails instead of silently truncating', async () => {
  for (const key of ['fields', 'items']) {
    const { adapter } = harness(s => { s[key][0].pageInfo.endCursor = null; });
    await assert.rejects(adapter.snapshot(config), /cursor missing/);
  }
});

test('malformed top-level pagination cannot silently truncate fields or items', async () => {
  for (const key of ['fields', 'items']) {
    const { adapter } = harness(s => { s[key][0].pageInfo = {}; });
    await assert.rejects(adapter.snapshot(config), /Incomplete|[Mm]alformed|[Ii]nvalid/);
  }
});

test('incomplete nested connections halt snapshot before unsafe scheduling', async () => {
  for (const key of ['fieldValues', 'labels', 'assignees', 'blockedBy', 'closedByPullRequestsReferences', 'subIssues']) {
    for (const value of [null, connection([], true, 'more')]) {
      const { adapter } = harness(s => {
        const item = s.items[0].nodes[0];
        if (key === 'fieldValues') item[key] = value;
        else item.content[key] = value;
      });
      await assert.rejects(adapter.snapshot(config), /Incomplete/);
    }
  }
});

test('malformed connections require nodes plus explicit boolean pagination metadata', () => {
  ensureComplete(connection([]), 'valid');
  for (const malformed of [undefined, {}, { nodes: [] }, { nodes: [], pageInfo: {} }, { nodes: [], pageInfo: { hasNextPage: null } }, { nodes: [], pageInfo: { hasNextPage: 0 } }, { nodes: null, pageInfo: { hasNextPage: false } }]) {
    assert.throws(() => ensureComplete(malformed, 'fixture'), /Incomplete|[Mm]alformed|[Ii]nvalid/);
  }
});

test('field decoding supports all projected types and preserves empty text and zero numbers', () => {
  assert.deepEqual(values([
    {}, { field: {} }, field('text', { text: '' }), field('choice', { name: 'Option' }),
    field('number', { number: 0 }), field('date', { date: '2026-11-04' }),
    field('iteration', { title: 'Cycle 8' }), field('unset', {}),
  ]), { text: '', choice: 'Option', number: 0, date: '2026-11-04', iteration: 'Cycle 8', unset: null });
});

test('REST issue reads paginate all pages and exclude PRs; mutations use stdin payloads', () => {
  const calls = [];
  const responses = [[[ { number: 1 }, { number: 2, pull_request: { url: 'pr' } } ], [{ number: 3 }]], { number: 5 }, { number: 6 }, { number: 6 }, { id: 70 }];
  const adapter = new GitHub((command, args, options) => { calls.push({ args, input: options.input && JSON.parse(options.input) }); return JSON.stringify(responses.shift()); });
  assert.deepEqual(adapter.allIssues('acme/engine'), [{ number: 1 }, { number: 3 }]);
  assert.deepEqual(adapter.issue('acme/engine', 5), { number: 5 });
  assert.deepEqual(adapter.createIssue('acme/engine', { title: 'Fix', body: 'Details' }), { number: 6 });
  assert.deepEqual(adapter.updateIssue('acme/engine', 6, { state: 'closed' }), { number: 6 });
  assert.deepEqual(adapter.comment('acme/engine', 6, 'Evidence'), { id: 70 });
  assert.deepEqual(calls, [
    { args: ['api', '--paginate', '--slurp', 'repos/acme/engine/issues?state=all&per_page=100'], input: undefined },
    { args: ['api', 'repos/acme/engine/issues/5'], input: undefined },
    { args: ['api', 'repos/acme/engine/issues', '--method', 'POST', '--input', '-'], input: { title: 'Fix', body: 'Details' } },
    { args: ['api', 'repos/acme/engine/issues/6', '--method', 'PATCH', '--input', '-'], input: { state: 'closed' } },
    { args: ['api', 'repos/acme/engine/issues/6/comments', '--method', 'POST', '--input', '-'], input: { body: 'Evidence' } },
  ]);
});

test('label discovery requests all REST pages and returns exact live names without writes', () => {
  const calls = [];
  const pages = [[{ name: 'task', color: 'ffffff' }, { name: 'Priority: Critical' }], [{ name: 'Release: Orchid' }]];
  const adapter = new GitHub((command, args, options) => {
    calls.push(args);
    assert.equal(command, 'gh');
    assert.deepEqual(args, ['api', '--paginate', '--slurp', 'repos/acme/engine/labels?per_page=100']);
    assert.equal(options.input, undefined);
    return JSON.stringify(pages);
  });
  assert.deepEqual(adapter.labels('acme/engine'), ['task', 'Priority: Critical', 'Release: Orchid']);
  assert.equal(calls.length, 1);
  assert.deepEqual(new GitHub(() => '[[]]').labels('acme/empty'), []);
  assert.throws(() => new GitHub(() => { throw Object.assign(new Error('denied'), { status: 1 }); }).labels('acme/engine'), /GitHub command failed/);
});

test('GraphQL mutations preserve live IDs and select option IDs rather than hardcoding values', () => {
  const calls = [];
  const adapter = new GitHub((command, args, options) => {
    assert.deepEqual(args, ['api', 'graphql', '--input', '-']);
    const payload = JSON.parse(options.input); calls.push(payload);
    if (payload.query.includes('addProjectV2ItemById')) return '{"data":{"addProjectV2ItemById":{"item":{"id":"new-item"}}}}';
    return '{"data":{"ok":true}}';
  });
  assert.equal(adapter.addItem('proj', 'issue'), 'new-item');
  assert.deepEqual(calls.at(-1).variables, { p: 'proj', c: 'issue' });
  assert.deepEqual(adapter.setField('proj', 'item', { id: 'text-field', dataType: 'TEXT' }, 'Cycle 9'), { ok: true });
  assert.deepEqual(calls.at(-1).variables, { p: 'proj', i: 'item', f: 'text-field', v: { text: 'Cycle 9' } });
  const select = { id: 'select-field', name: 'Status', dataType: 'SINGLE_SELECT', options: [{ id: 'not-this', name: 'Other' }, { id: 'live-option', name: 'Building' }] };
  adapter.setField('proj', 'item', select, 'Building');
  assert.deepEqual(calls.at(-1).variables, { p: 'proj', i: 'item', f: 'select-field', v: { singleSelectOptionId: 'live-option' } });
  adapter.addDependency('issue', 'blocker');
  assert.deepEqual(calls.at(-1).variables, { i: 'issue', b: 'blocker' });
  assert.match(calls.at(-1).query, /addBlockedBy/);
  const before = calls.length;
  assert.throws(() => adapter.setField('proj', 'item', select, 'building'), /Unknown Status option/);
  assert.throws(() => adapter.setField('proj', 'item', { id: 'number', dataType: 'NUMBER' }, 5), /Unsupported writable field/);
  assert.equal(calls.length, before);
});
