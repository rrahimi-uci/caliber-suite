import test from 'node:test';
import assert from 'node:assert/strict';
import {
  context,
  createPlannedIssue,
  issuePlan,
  transition,
  transitionPlan,
} from '../lib/operations.mjs';

const config = () => ({
  repository: 'team/product',
  project: { owner: 'team', number: 73 },
  fields: {
    status: 'Workflow', readiness: 'Can start', release: 'Ship', cycle: 'Iteration',
    priority: 'Rank', kind: 'Class', humanInput: 'Decision',
  },
  statuses: { todo: 'Queued', active: 'Building', done: 'Shipped' },
});

function fixture({ cycle = 'Sprint 8', state = 'OPEN', stateReason = 'REOPENED' } = {}) {
  const cfg = config();
  const capturedAt = '2026-11-04T00:00:00Z';
  const milestone = { id: 'milestone-9', number: 9, title: 'Launch window', state: 'OPEN', description: 'Acceptance' };
  const snapshot = {
    capturedAt,
    repository: { nameWithOwner: cfg.repository, url: 'https://example.test/team/product', defaultBranch: 'trunk', headOid: 'abc' },
    project: { id: 'project-73', number: 73, title: 'Engineering', url: 'https://example.test/project/73', readme: '' },
    fields: Object.entries(cfg.fields).map(([role, name], index) => ({
      id: `dynamic-${index}`,
      name,
      dataType: ['status', 'release'].includes(role) ? 'SINGLE_SELECT' : 'TEXT',
      ...(['status', 'release'].includes(role)
        ? { options: (role === 'status' ? Object.values(cfg.statuses) : ['Orchid', 'Future'])
          .map((option, optionIndex) => ({ id: `${index}-${optionIndex}`, name: option })) }
        : {}),
    })),
    milestones: [milestone],
    items: [],
  };
  const item = (number = 1, overrides = {}) => {
    const { fields = {}, issue = {}, ...itemOverrides } = overrides;
    const value = {
      id: `item-${number}`,
      isArchived: false,
      fields: {
        Workflow: 'Queued', 'Can start': 'Ready', Ship: 'Orchid', Iteration: cycle,
        Rank: 'P1', Class: 'Task', Decision: 'None', ...fields,
      },
      issue: {
        id: `issue-${number}`,
        number,
        title: `Implement ${number}`,
        body: '',
        state,
        stateReason,
        url: `https://example.test/team/product/issues/${number}`,
        updatedAt: capturedAt,
        repository: cfg.repository,
        labels: [],
        assignees: [],
        milestone,
        blockedBy: [],
        prs: [],
        subIssues: [],
        parent: null,
        ...issue,
      },
      ...itemOverrides,
    };
    snapshot.items.push(value);
    return value;
  };
  return { config: cfg, snapshot, milestone, item };
}

const payload = (overrides = {}) => ({
  title: 'Implement the release gate',
  kind: 'Task',
  objective: 'Make the release gate observable',
  context: 'The current gate has no deterministic evidence',
  acceptance: ['The gate reports a stable result'],
  tests: ['Run the focused regression suite'],
  components: ['.agents/lib/operations.mjs'],
  complexity: 'Small',
  parallelization: 'Serial',
  priority: 'P1',
  dependencies: [],
  ...overrides,
});

const labelConfig = () => ({
  task: 'task',
  epic: 'epic',
  priorities: { P0: 'priority-p0', P1: 'priority-p1', P2: 'priority-p2', P3: 'priority-p3' },
  releases: { 'Preview 1': ['release:preview-1', 'stage:beta'] },
});

const availableLabelNames = () => [
  'task', 'epic', 'priority-p0', 'priority-p1', 'priority-p2', 'priority-p3',
  'release:preview-1', 'stage:beta',
];

function labelFixture() {
  const state = fixture();
  const releaseField = state.snapshot.fields.find(field => field.name === state.config.fields.release);
  releaseField.options[0].name = 'Preview 1';
  const work = state.item();
  work.fields.Ship = 'Preview 1';
  state.config.labels = labelConfig();
  return state;
}

function githubHarness({ existing = [], current, snapshot, failures = {}, availableLabels = [] } = {}) {
  const events = [];
  const gh = {
    allIssues(repository) {
      events.push(['allIssues', repository]);
      return existing;
    },
    labels(repository) {
      events.push(['labels', repository]);
      return availableLabels;
    },
    snapshot(config) {
      events.push(['snapshot', config.repository]);
      if (failures.snapshot) throw new Error(failures.snapshot);
      if (!snapshot) throw new Error('test harness snapshot was not provided');
      return snapshot;
    },
    createIssue(repository, plan) {
      events.push(['createIssue', repository, plan]);
      if (failures.createIssue) throw new Error(failures.createIssue);
      return { number: 701, node_id: 'new-issue', html_url: 'https://example.test/issues/701' };
    },
    addItem(projectId, issueId) {
      events.push(['addItem', projectId, issueId]);
      if (failures.addItem) throw new Error(failures.addItem);
      return 'new-item';
    },
    setField(projectId, itemId, field, value) {
      events.push(['setField', projectId, itemId, field.name, value]);
      if (failures.setField === field.name) throw new Error(`field ${field.name} failed`);
      return { ok: true };
    },
    addDependency(issueId, blockerId) {
      events.push(['addDependency', issueId, blockerId]);
      if (failures.addDependency) throw new Error(failures.addDependency);
      return { ok: true };
    },
    issue(repository, number) {
      events.push(['issue', repository, number]);
      if (failures.issue) throw new Error(failures.issue);
      return { ...current };
    },
    updateIssue(repository, number, changes) {
      events.push(['updateIssue', repository, number, changes]);
      if (failures.updateIssue) throw new Error(failures.updateIssue);
      return { ok: true };
    },
    comment(repository, number, body) {
      events.push(['comment', repository, number, body]);
      if (failures.comment) throw new Error(failures.comment);
      return { ok: true };
    },
  };
  return { gh, events };
}

function currentFor(item, overrides = {}) {
  return {
    updated_at: item.issue.updatedAt,
    state: item.issue.state.toLowerCase(),
    state_reason: item.issue.stateReason?.toLowerCase(),
    stateReason: item.issue.stateReason,
    assignees: [],
    ...overrides,
  };
}

test('context resolves slash commands and rejects unknown workflows', () => {
  const { config: cfg, snapshot, item } = fixture();
  item();
  const result = context(snapshot, cfg, '/plan', 'release:orchid');
  assert.equal(result.target.key, 'release:Orchid');
  assert.equal(result.instructions.workflow, '.agents/workflows/planning.md');
  assert.match(result.notice, /not an executed/);
  assert.throws(() => context(snapshot, cfg, 'unknown', 'Orchid'), /Unknown workflow/);
});

test('issuePlan validates a complete payload, uses live aliases, and preserves dynamic IDs', () => {
  const { config: cfg, snapshot, item } = fixture();
  item(1);
  const dependency = { number: 88, node_id: 'dependency-88', title: 'Blocker', state: 'closed' };
  const plan = issuePlan(snapshot, cfg, ' RELEASE:orchid ', payload({ dependencies: [88], human_input: 'H01: acceptance' }), [dependency]);
  assert.equal(plan.action, 'create');
  assert.equal(plan.issue.milestone, 9);
  assert.equal(plan.assignments.release, 'Orchid');
  assert.equal(plan.assignments.cycle, 'Sprint 8');
  assert.equal(plan.assignments.humanInput, 'H01: acceptance');
  assert.equal(plan.assignments.kind, 'Task');
  assert.deepEqual(plan.dependencies, [dependency]);
  assert.equal(plan.fields.status.id, 'dynamic-0');
  assert.match(plan.issue.body, /## Dependencies\n\n- #88/);
  assert.match(plan.issue.body, /Delivery cycle: Sprint 8/);
  assert.match(plan.issue.body, /caliber-engineering-meta:/);
});

test('issuePlan defaults optional human input to an assessed-safe value and rejects invalid supplied text', () => {
  const { config: cfg, snapshot, item } = fixture();
  item();
  const plan = issuePlan(snapshot, cfg, 'Orchid', payload(), []);
  assert.equal(plan.assignments.humanInput, 'Not assessed');
  for (const human_input of [7, '', null]) {
    assert.throws(() => issuePlan(snapshot, cfg, 'Orchid', payload({ human_input }), []), /human_input/);
  }
});

test('issuePlan maps configured task, epic, priority, and release labels against live names', () => {
  const { config: cfg, snapshot } = labelFixture();
  const taskPlan = issuePlan(snapshot, cfg, 'Preview 1', payload({ priority: 'P3' }), [], availableLabelNames());
  assert.deepEqual(taskPlan.issue.labels, ['task', 'priority-p3', 'release:preview-1', 'stage:beta']);
  const epicPlan = issuePlan(snapshot, cfg, 'Preview 1', payload({ kind: 'Epic', priority: 'P0' }), [], availableLabelNames());
  assert.deepEqual(epicPlan.issue.labels, ['epic', 'priority-p0', 'release:preview-1', 'stage:beta']);
});

test('issuePlan rejects missing or drifted configured label mappings before creating an issue', () => {
  const cases = [
    [labels => { labels.releases['Preview 1'] = []; }, /Configure discovered labels/],
    [labels => { labels.task = 'renamed-task'; }, /missing or renamed/],
    [labels => { labels.priorities.P1 = 'priority-renamed'; }, /missing or renamed/],
    [labels => { labels.releases['Preview 1'] = ['release:renamed']; }, /missing or renamed/],
    [labels => { labels.releases['Preview 1'] = ['release:preview-1', 'stage:beta', 'stage:beta']; }, undefined],
  ];
  for (const [mutate, error] of cases) {
    const { config: cfg, snapshot } = labelFixture();
    mutate(cfg.labels);
    if (error) assert.throws(() => issuePlan(snapshot, cfg, 'Preview 1', payload(), [], availableLabelNames()), error);
    else assert.deepEqual(issuePlan(snapshot, cfg, 'Preview 1', payload(), [], availableLabelNames()).issue.labels, ['task', 'priority-p1', 'release:preview-1', 'stage:beta']);
  }
  const { config: cfg, snapshot } = labelFixture();
  assert.throws(() => issuePlan(snapshot, cfg, 'Preview 1', payload(), [], ['task', 'priority-p1', 'release:preview-1']), /missing or renamed/);
});

test('issuePlan permits an unscheduled release or milestone with no observed cycle', () => {
  const { config: cfg, snapshot, item } = fixture({ cycle: '' });
  item();
  const plan = issuePlan(snapshot, cfg, 'milestone:#9', payload(), []);
  assert.equal(plan.issue.milestone, 9);
  assert.equal(plan.assignments.cycle, undefined);
  assert.match(plan.issue.body, /Delivery cycle: Unscheduled/);
  assert.throws(() => issuePlan(snapshot, cfg, 'milestone:#9', payload({ cycle: 'Invented cycle' }), []), /existing delivery cycle/);
});

test('issuePlan requires explicit selection when multiple live cycles exist', () => {
  const { config: cfg, snapshot, item } = fixture();
  item(1);
  item(2, { fields: { Iteration: 'Sprint 9' } });
  assert.throws(() => issuePlan(snapshot, cfg, 'Orchid', payload(), []), /existing delivery cycle/);
  const selected = issuePlan(snapshot, cfg, 'Orchid', payload({ cycle: 'Sprint 9' }), []);
  assert.equal(selected.assignments.cycle, 'Sprint 9');
  const cycleTarget = issuePlan(snapshot, cfg, 'cycle:Sprint 8', payload(), []);
  assert.equal(cycleTarget.assignments.cycle, 'Sprint 8');
  assert.throws(() => issuePlan(snapshot, cfg, 'cycle:Sprint 8', payload({ cycle: 'Sprint 9' }), []), /existing delivery cycle/);
});

test('issuePlan rejects malformed payloads, unsafe paths, invalid milestones, and unknown dependencies', () => {
  const mutations = [
    [p => { p.title = ''; }, /title/],
    [p => { p.title = 'line\nbreak'; }, /single line/],
    [p => { p.title = 'x'.repeat(241); }, /240/],
    [p => { p.kind = 'Feature'; }, /issue kind/],
    [p => { p.objective = ''; }, /objective/],
    [p => { p.context = ''; }, /context/],
    [p => { p.acceptance = []; }, /acceptance/],
    [p => { p.tests = [null]; }, /tests/],
    [p => { p.components = 'not-a-list'; }, /components/],
    [p => { p.complexity = ''; }, /complexity/],
    [p => { p.parallelization = ''; }, /parallelization/],
    [p => { p.priority = 'P4'; }, /priority/],
    [p => { p.dependencies = 'none'; }, /dependencies/],
    [p => { p.dependencies = [0]; }, /dependencies/],
    [p => { p.dependencies = [2, 2]; }, /Duplicate dependency/],
    [p => { p.owned_paths = ['/tmp']; }, /repository-relative/],
    [p => { p.owned_paths = ['../outside']; }, /repository-relative/],
    [p => { p.owned_paths = ['src\\file']; }, /repository-relative/],
  ];
  for (const [mutate, error] of mutations) {
    const { config: cfg, snapshot, item } = fixture();
    item();
    const candidate = payload();
    mutate(candidate);
    assert.throws(() => issuePlan(snapshot, cfg, 'Orchid', candidate, []), error);
  }
  {
    const { config: cfg, snapshot, item } = fixture();
    item();
    assert.throws(() => issuePlan(snapshot, cfg, 'Orchid', payload({ dependencies: [42] }), []), /Unknown dependency/);
  }
  {
    const { config: cfg, snapshot, item, milestone } = fixture();
    item();
    snapshot.milestones.push({ ...milestone, id: 'closed', number: 10, title: 'Closed window', state: 'CLOSED' });
    assert.throws(() => issuePlan(snapshot, cfg, 'Orchid', payload({ milestone: 10 }), []), /open native milestone/);
  }
  {
    const { config: cfg, snapshot, item } = fixture();
    item();
    assert.throws(() => issuePlan(snapshot, cfg, 'Orchid', payload({ milestone: 404 }), []), /open native milestone/);
  }
});

test('issuePlan enforces release and target consistency without inventing aliases', () => {
  const { config: cfg, snapshot, item } = fixture();
  item();
  for (const [argument, changes] of [
    ['Future', {}, /existing release/],
    ['Orchid', { release: 'Future' }, /existing release/],
    ['Orchid', { cycle: 'No such cycle' }, /existing delivery cycle/],
    ['cycle:Sprint 8', { release: 'Future' }, /existing release/],
  ]) {
    assert.throws(() => issuePlan(snapshot, cfg, argument, payload(changes), []), changes.release ? /existing release/ : changes.cycle ? /existing delivery cycle/ : /Empty live/);
  }
  const targetRelease = issuePlan(snapshot, cfg, 'release:Orchid', payload({ release: 'Orchid' }), []);
  assert.equal(targetRelease.assignments.release, 'Orchid');
});

test('issuePlan detects semantic duplicates including closed issues and ambiguous matches', () => {
  const { config: cfg, snapshot, item } = fixture();
  item();
  const first = issuePlan(snapshot, cfg, 'Orchid', payload(), []);
  const closed = { number: 501, node_id: 'closed-501', title: '  IMPLEMENT THE RELEASE GATE ', body: '', state: 'closed' };
  const duplicate = issuePlan(snapshot, cfg, 'Orchid', payload(), [closed]);
  assert.equal(duplicate.action, 'duplicate');
  assert.equal(duplicate.duplicate, closed);
  const fingerprintMatch = issuePlan(snapshot, cfg, 'Orchid', payload({ title: 'Different title' }), [{
    number: 502,
    node_id: 'fingerprint-502',
    title: 'Different title',
    body: `<!-- caliber-engineering-meta:${JSON.stringify({ fingerprint: first.fingerprint })} -->`,
  }]);
  assert.equal(fingerprintMatch.action, 'duplicate');
  assert.throws(() => issuePlan(snapshot, cfg, 'Orchid', payload(), [closed, { ...closed, number: 503, node_id: 'closed-503' }]), /Ambiguous duplicates/);
});

test('createPlannedIssue is read-only for dry runs and duplicates', async () => {
  const { config: cfg, snapshot, item } = fixture();
  item();
  const dry = githubHarness();
  const result = await createPlannedIssue(dry.gh, snapshot, cfg, 'Orchid', payload(), false);
  assert.equal(result.applied, false);
  assert.match(result.notice, /Dry run/);
  assert.deepEqual(dry.events, [['allIssues', cfg.repository]]);
  const duplicate = githubHarness({ existing: [{ number: 8, node_id: 'existing-8', title: payload().title, body: '', state: 'closed' }] });
  const preserved = await createPlannedIssue(duplicate.gh, snapshot, cfg, 'Orchid', payload(), true);
  assert.equal(preserved.action, 'duplicate');
  assert.equal(preserved.applied, false);
  assert.deepEqual(duplicate.events, [['allIssues', cfg.repository]]);
});

test('createPlannedIssue writes in dependency order and emits all dynamic assignments', async () => {
  const { config: cfg, snapshot, item } = fixture();
  item();
  const dependency = { number: 88, node_id: 'dependency-88', title: 'Blocker', state: 'closed' };
  const { gh, events } = githubHarness({ existing: [dependency] });
  const result = await createPlannedIssue(gh, snapshot, cfg, 'Orchid', payload({ dependencies: [88], human_input: 'None pending' }), true);
  assert.deepEqual(result, {
    action: 'created', applied: true, number: 701, url: 'https://example.test/issues/701', itemId: 'new-item', fingerprint: result.fingerprint,
  });
  assert.deepEqual(events.map(event => event[0]), ['allIssues', 'createIssue', 'addItem', ...Array(7).fill('setField'), 'addDependency']);
  assert.equal(events[1][2].milestone, 9);
  assert.equal(events[2][2], 'new-issue');
  assert.equal(events.at(-1)[1], 'new-issue');
  assert.equal(events.at(-1)[2], 'dependency-88');
});

test('createPlannedIssue discovers labels only when configured and sends the validated labels on success', async () => {
  const { config: cfg, snapshot } = labelFixture();
  const { gh, events } = githubHarness({ availableLabels: availableLabelNames() });
  const result = await createPlannedIssue(gh, snapshot, cfg, 'Preview 1', payload({ priority: 'P2' }), true);
  assert.equal(result.applied, true);
  assert.deepEqual(events.slice(0, 3).map(event => event[0]), ['allIssues', 'labels', 'createIssue']);
  assert.deepEqual(events[1], ['labels', cfg.repository]);
  assert.deepEqual(events[2][2].labels, ['task', 'priority-p2', 'release:preview-1', 'stage:beta']);
});

test('createPlannedIssue reports incomplete writes with repair guidance', async () => {
  const { config: cfg, snapshot, item } = fixture();
  item();
  for (const failures of [{ createIssue: 'create failed' }, { addItem: 'item failed' }, { setField: 'Workflow' }, { addDependency: 'dependency failed' }]) {
    const dependency = { number: 88, node_id: 'dependency-88', title: 'Blocker', state: 'closed' };
    const { gh, events } = githubHarness({ existing: [dependency], failures });
    const changes = failures.addDependency ? { dependencies: [88] } : {};
    await assert.rejects(createPlannedIssue(gh, snapshot, cfg, 'Orchid', payload(changes), true), error => {
      assert.match(error.message, /Issue write incomplete/);
      if (!failures.createIssue) assert.match(error.message, /repair existing #701/);
      return true;
    });
    assert.equal(events[0][0], 'allIssues');
  }
});

test('transitionPlan covers ordinary phases, evidence, and live eligibility gates', () => {
  const { config: cfg, snapshot, item } = fixture();
  const work = item(1);
  assert.equal(transitionPlan(snapshot, cfg, 'Orchid', 1, 'planned').assignments.readiness, 'Planned');
  assert.equal(transitionPlan(snapshot, cfg, 'Orchid', 1, 'ready', { evidence: ['scope checked'] }).assignments.readiness, 'Ready');
  assert.equal(transitionPlan(snapshot, cfg, 'Orchid', 1, 'claim', { owner: 'agent-1', branch: 'feat/agent-1' }).assignments.status, 'Building');
  work.fields.Workflow = 'Building';
  work.issue.prs = [{ number: 44, state: 'OPEN', mergedAt: null }];
  assert.equal(transitionPlan(snapshot, cfg, 'Orchid', 1, 'review', { evidence: ['PR is open'] }).assignments.readiness, 'In Review');
  assert.equal(transitionPlan(snapshot, cfg, 'Orchid', 1, 'qa', { evidence: ['tests passed'] }).assignments.readiness, 'QA');
  work.fields.Workflow = 'Queued';
  assert.equal(transitionPlan(snapshot, cfg, 'Orchid', 1, 'blocked', { reason: 'Waiting for a decision' }).assignments.readiness, 'Blocked');
  assert.throws(() => transitionPlan(snapshot, cfg, 'Orchid', 1, 'unknown', {}), /Unknown phase/);
  assert.throws(() => transitionPlan(snapshot, cfg, 'Orchid', 999, 'planned', {}), /not in the live target/);
});

test('transitionPlan rejects unsafe phase transitions and requires meaningful payloads', () => {
  const { config: cfg, snapshot, item } = fixture();
  item(1, { issue: { state: 'CLOSED', stateReason: 'COMPLETED' } });
  assert.throws(() => transitionPlan(snapshot, cfg, 'Orchid', 1, 'claim', { owner: 'agent', branch: 'feat/agent' }), /Only open/);
  assert.throws(() => transitionPlan(snapshot, cfg, 'Orchid', 1, 'planned', {}), /Only open/);

  const ready = fixture();
  ready.item(1, { fields: { Decision: 'None' } });
  ready.snapshot.items[0].issue.assignees = ['existing'];
  assert.throws(() => transitionPlan(ready.snapshot, ready.config, 'Orchid', 1, 'planned', {}), /Do not reset claimed/);

  const pr = fixture();
  pr.item(1, { issue: { prs: [{ number: 4, state: 'OPEN' }] } });
  assert.throws(() => transitionPlan(pr.snapshot, pr.config, 'Orchid', 1, 'planned', {}), /Do not reset claimed/);

  const active = fixture();
  active.item(1, { fields: { Workflow: 'Building' } });
  assert.throws(() => transitionPlan(active.snapshot, active.config, 'Orchid', 1, 'planned', {}), /Do not reset claimed/);

  const noEvidence = fixture();
  noEvidence.item();
  assert.throws(() => transitionPlan(noEvidence.snapshot, noEvidence.config, 'Orchid', 1, 'ready', {}), /evidence/);
  assert.throws(() => transitionPlan(noEvidence.snapshot, noEvidence.config, 'Orchid', 1, 'blocked', {}), /reason/);
  assert.throws(() => transitionPlan(noEvidence.snapshot, noEvidence.config, 'Orchid', 1, 'review', { evidence: ['x'] }), /open linked PR/);
  assert.throws(() => transitionPlan(noEvidence.snapshot, noEvidence.config, 'Orchid', 1, 'qa', { evidence: ['x'] }), /claimed In Progress/);
  assert.throws(() => transitionPlan(noEvidence.snapshot, noEvidence.config, 'Orchid', 1, 'review', { evidence: [] }), /evidence/);

  for (const [owner, branch, error] of [
    ['', 'feat/x', /owner/], ['bad owner', 'feat/x', /owner/], ['agent', '', /branch/],
    ['agent', 'main', /dedicated branch/], ['agent', 'master', /dedicated branch/],
    ['agent', 'trunk', /dedicated branch/], ['agent', 'refs/heads/main', /dedicated branch/],
    ['agent', 'refs/heads/master', /dedicated branch/], ['agent', 'refs/heads/trunk', /dedicated branch/],
    ['agent', 'refs/heads/', /dedicated branch/], ['agent', 'bad branch', /dedicated branch/],
  ]) {
    const candidate = fixture(); candidate.item();
    assert.throws(() => transitionPlan(candidate.snapshot, candidate.config, 'Orchid', 1, 'claim', { owner, branch }), error);
  }
});

test('Done requires a completed closed issue and evidence', () => {
  const open = fixture({ state: 'OPEN', stateReason: 'REOPENED' });
  open.item();
  assert.throws(() => transitionPlan(open.snapshot, open.config, 'Orchid', 1, 'done', { evidence: ['evidence'] }), /closed as COMPLETED/);
  const notPlanned = fixture({ state: 'CLOSED', stateReason: 'NOT_PLANNED' });
  notPlanned.item();
  assert.throws(() => transitionPlan(notPlanned.snapshot, notPlanned.config, 'Orchid', 1, 'done', { evidence: ['not acceptance'] }), /COMPLETED/);
  const completed = fixture({ state: 'CLOSED', stateReason: 'COMPLETED' });
  completed.item();
  const plan = transitionPlan(completed.snapshot, completed.config, 'Orchid', 1, 'done', { evidence: ['acceptance independently verified'] });
  assert.equal(plan.assignments.status, 'Shipped');
  assert.equal(plan.assignments.readiness, 'Done');
  assert.throws(() => transitionPlan(completed.snapshot, completed.config, 'Orchid', 1, 'done', {}), /evidence/);
});

test('transition dry-run performs no GitHub mutation and apply rechecks the live issue', async () => {
  const { config: cfg, snapshot, item } = fixture();
  const work = item();
  const dry = githubHarness({ current: currentFor(work), snapshot });
  const planned = await transition(dry.gh, snapshot, cfg, 'Orchid', 1, 'planned', {}, false);
  assert.equal(planned.applied, false);
  assert.equal(dry.events.length, 0);

  const stale = githubHarness({ current: currentFor(work, { updated_at: 'stale' }), snapshot });
  await assert.rejects(transition(stale.gh, snapshot, cfg, 'Orchid', 1, 'planned', {}, true), /changed since snapshot/);
  assert.deepEqual(stale.events.map(event => event[0]), ['snapshot', 'issue']);

  const wrongState = githubHarness({ current: currentFor(work, { state: 'closed' }), snapshot });
  await assert.rejects(transition(wrongState.gh, snapshot, cfg, 'Orchid', 1, 'planned', {}, true), /changed since snapshot/);
});

test('transition refuses concurrent claims before writing comments or fields', async () => {
  const { config: cfg, snapshot, item } = fixture();
  const work = item();
  const race = githubHarness({ current: currentFor(work, { assignees: ['other-agent'] }), snapshot });
  await assert.rejects(transition(race.gh, snapshot, cfg, 'Orchid', 1, 'claim', { owner: 'agent-1', branch: 'feat/agent-1' }, true), /claimed concurrently/);
  assert.deepEqual(race.events.map(event => event[0]), ['snapshot', 'issue']);
});

test('applied claims authorize against a fresh snapshot and perform no REST writes when start eligibility changes', async () => {
  const initial = fixture();
  const work = initial.item();
  for (const change of [
    fresh => { fresh.items[0].fields.Decision = 'Approval pending'; },
    fresh => { fresh.items[0].issue.blockedBy = [{ number: 99, repository: initial.config.repository, state: 'OPEN' }]; },
  ]) {
    const fresh = structuredClone(initial.snapshot);
    change(fresh);
    const harness = githubHarness({ snapshot: fresh, current: currentFor(work) });
    await assert.rejects(
      transition(harness.gh, initial.snapshot, initial.config, 'Orchid', 1, 'claim', { owner: 'agent-1', branch: 'feat/agent-1' }, true),
      /Task is no longer eligible to claim/,
    );
    assert.deepEqual(harness.events.map(event => event[0]), ['snapshot']);
  }
});

test('transition writes claim, blocked, review, QA, and Done comments with state fields', async () => {
  const cases = [
    { phase: 'claim', item: {}, payload: { owner: 'agent-1', branch: 'feat/agent-1' }, current: {}, marker: 'Owner: @agent-1' },
    { phase: 'blocked', item: {}, payload: { reason: 'Waiting for product decision' }, current: {}, marker: 'Waiting for product decision' },
    { phase: 'review', item: { fields: { Workflow: 'Building' }, issue: { prs: [{ number: 44, state: 'OPEN' }] } }, payload: { evidence: ['PR #44 is open'] }, current: {}, marker: 'PR #44 is open' },
    { phase: 'qa', item: { fields: { Workflow: 'Building' }, issue: { prs: [{ number: 44, state: 'OPEN' }] } }, payload: { evidence: ['focused tests passed'] }, current: {}, marker: 'focused tests passed' },
    { phase: 'done', item: { issue: { state: 'CLOSED', stateReason: 'COMPLETED' } }, payload: { evidence: ['acceptance verified'] }, current: { state: 'closed', state_reason: 'completed', stateReason: 'COMPLETED' }, marker: 'acceptance verified' },
  ];
  for (const candidate of cases) {
    const { config: cfg, snapshot, item } = fixture({
      state: candidate.item.issue?.state || 'OPEN',
      stateReason: candidate.item.issue?.stateReason || 'REOPENED',
    });
    const work = item(1, candidate.item);
    const { gh, events } = githubHarness({ current: currentFor(work, candidate.current), snapshot });
    const result = await transition(gh, snapshot, cfg, 'Orchid', 1, candidate.phase, candidate.payload, true);
    assert.equal(result.applied, true);
    assert.equal(events[0][0], 'snapshot');
    assert.equal(events.find(event => event[0] === 'comment')[3].includes(candidate.marker), true);
    assert.ok(events.some(event => event[0] === 'setField'));
    if (candidate.phase === 'claim') assert.ok(events.some(event => event[0] === 'updateIssue'));
  }
});

test('transition rechecks completion state and reports non-transactional write failures', async () => {
  const completed = fixture({ state: 'CLOSED', stateReason: 'COMPLETED' });
  const work = completed.item();
  const notPlanned = githubHarness({ current: currentFor(work, { state_reason: 'not_planned', stateReason: 'NOT_PLANNED' }), snapshot: completed.snapshot });
  await assert.rejects(transition(notPlanned.gh, completed.snapshot, completed.config, 'Orchid', 1, 'done', { evidence: ['evidence'] }, true), /changed since snapshot/);

  const failed = fixture();
  const failedWork = failed.item();
  const { gh, events } = githubHarness({ current: currentFor(failedWork), snapshot: failed.snapshot, failures: { setField: 'Workflow' } });
  await assert.rejects(transition(gh, failed.snapshot, failed.config, 'Orchid', 1, 'planned', {}, true), /Transition incomplete for #1/);
  assert.ok(events.some(event => event[0] === 'comment'));
});
