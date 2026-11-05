import { createHash } from 'node:crypto';
import { analyzeScope, resolveTarget, validateMapping } from './model.mjs';

export const COMMANDS = ['architect', 'plan', 'develop', 'review', 'critique', 'bug-bash', 'release-check'];
const WORKFLOWS = ['architecture', 'planning', 'development', 'review', 'milestone-critique', 'bug-bash', 'release-check'];
const ROLES = ['architect', 'planner', 'developer', 'reviewer', 'critic', 'bug-basher', 'qa'];
const required = (value, name) => {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`${name} must be nonempty text`);
  return value.trim();
};
const list = (value, name) => {
  if (!Array.isArray(value) || !value.length) throw new Error(`${name} must be a nonempty array`);
  return value.map(x => required(x, name));
};
const normalize = value => value.trim().replace(/\s+/g, ' ').toLowerCase();

export function context(snapshot, config, command, argument) {
  const index = COMMANDS.indexOf(command.replace(/^\//, ''));
  if (index < 0) throw new Error(`Unknown workflow: ${command}`);
  const target = resolveTarget(snapshot, argument, config);
  const analysis = analyzeScope(snapshot, target, config);
  return {
    capturedAt: snapshot.capturedAt, repository: snapshot.repository, project: snapshot.project,
    instructions: { policy: '.agents/engineering-policy.md', command: `.agents/commands/${COMMANDS[index]}.md`, workflow: `.agents/workflows/${WORKFLOWS[index]}.md`, role: `.agents/roles/${ROLES[index]}.md` },
    target, analysis,
    notice: 'Live context, not an executed agent workflow or release approval. Read complete issues and linked evidence. Refresh before writes and after integration.',
  };
}

export function issuePlan(snapshot, config, argument, payload, existing, availableLabels = []) {
  const fields = validateMapping(snapshot, config);
  const target = resolveTarget(snapshot, argument, config);
  const title = required(payload.title, 'title');
  if (title.length > 240 || /[\r\n]/.test(title)) throw new Error('title must be a single line up to 240 characters');
  if (!['Task', 'Bug', 'Review', 'Critique', 'Architecture', 'Epic'].includes(payload.kind)) throw new Error('Invalid issue kind');
  const objective = required(payload.objective, 'objective');
  const details = required(payload.context, 'context');
  const acceptance = list(payload.acceptance, 'acceptance');
  const tests = list(payload.tests, 'tests');
  const components = list(payload.components, 'components');
  const complexity = required(payload.complexity, 'complexity');
  const parallelization = required(payload.parallelization, 'parallelization');
  if (!/^P[0-3]$/.test(payload.priority)) throw new Error('priority must be P0, P1, P2 or P3');
  if (!Array.isArray(payload.dependencies) || payload.dependencies.some(n => !Number.isSafeInteger(n) || n < 1)) throw new Error('dependencies must contain positive issue numbers');
  if (new Set(payload.dependencies).size !== payload.dependencies.length) throw new Error('Duplicate dependency');
  const dependencies = payload.dependencies.map(number => {
    const found = existing.find(issue => issue.number === number);
    if (!found?.node_id) throw new Error(`Unknown dependency #${number}`);
    return found;
  });
  const milestone = payload.milestone === undefined && target.milestones.length === 1
    ? target.milestones[0] : target.milestones.find(m => m.number === payload.milestone);
  if (!milestone || milestone.state.toUpperCase() !== 'OPEN') throw new Error('Select an open native milestone number belonging to this target');
  const releases = [...new Set(target.items.filter(i => i.issue.milestone?.number === milestone.number).map(i => i.fields[fields.release.name]).filter(Boolean))];
  const release = payload.release || (target.kind === 'release' ? target.name : releases.length === 1 ? releases[0] : null);
  if (!release || !releases.includes(release) || !fields.release.options.some(x => x.name === release) || (target.kind === 'release' && release !== target.name)) throw new Error('Select an existing release consistent with the target and milestone');
  const cycles = [...new Set(target.items.filter(i => i.issue.milestone?.number === milestone.number && i.fields[fields.release.name] === release).map(i => i.fields[fields.cycle.name]).filter(Boolean))];
  const cycle = payload.cycle || (target.kind === 'cycle' ? target.name : cycles.length === 1 ? cycles[0] : null);
  if ((cycle && !cycles.includes(cycle)) || (!cycle && cycles.length > 1) || (target.kind === 'cycle' && cycle !== target.name)) throw new Error('Select an existing delivery cycle consistent with the target and milestone');
  let labels;
  if (config.labels) {
    const releaseLabels = config.labels.releases?.[release];
    if (!Array.isArray(releaseLabels) || !releaseLabels.length) throw new Error(`Configure discovered labels for release ${release}`);
    labels = [...new Set([config.labels[payload.kind === 'Epic' ? 'epic' : 'task'], config.labels.priorities?.[payload.priority], ...releaseLabels])];
    if (labels.some(label => typeof label !== 'string' || !availableLabels.includes(label))) throw new Error('Configured issue labels are missing or renamed; discover current labels and repair mapping before writing');
  }
  const owned = payload.owned_paths || [];
  if (!Array.isArray(owned) || owned.some(p => typeof p !== 'string' || !p.trim() || p.startsWith('/') || /[\\:\x00-\x1f]/.test(p) || p.split('/').some(x => ['.', '..'].includes(x)))) throw new Error('owned_paths must be repository-relative paths');
  // Stable across release/cycle/milestone aliases for the same work. Semantic duplicates still need agent review.
  const fingerprint = createHash('sha256').update(JSON.stringify([config.repository, milestone.number, payload.kind, normalize(title)])).digest('hex');
  const marker = `<!-- caliber-engineering-meta:${JSON.stringify({ fingerprint, kind: payload.kind, owned_paths: owned })} -->`;
  const duplicate = existing.filter(issue => normalize(issue.title) === normalize(title) || issue.body?.includes(`"fingerprint":"${fingerprint}"`));
  if (duplicate.length > 1) throw new Error(`Ambiguous duplicates: ${duplicate.map(x => `#${x.number}`).join(', ')}`);
  const bullets = xs => xs.map(x => `- ${x}`).join('\n');
  const body = `${marker}\n\n## Objective\n\n${objective}\n\n## Context and evidence\n\n${details}\n\n## Acceptance criteria\n\n${bullets(acceptance)}\n\n## Required tests and QA\n\n${bullets(tests)}\n\n## Implementation boundaries\n\n${bullets(components)}\n\n## Dependencies\n\n${dependencies.length ? bullets(dependencies.map(i => `#${i.number}`)) : 'None.'}\n\n## Execution\n\n- Kind: ${payload.kind}\n- Target: ${target.key}\n- Native milestone: ${milestone.title}\n- Release: ${release}\n- Delivery cycle: ${cycle || 'Unscheduled'}\n- Priority/severity: ${payload.priority}\n- Estimated complexity: ${complexity}\n- Parallelization: ${parallelization}\n\nHuman decisions must be recorded before moving this issue to Ready.\n`;
  return { action: duplicate.length ? 'duplicate' : 'create', duplicate: duplicate[0] || null, fingerprint, issue: { title, body, milestone: milestone.number, ...(labels ? { labels } : {}) }, dependencies, assignments: { release, ...(cycle ? { cycle } : {}), priority: payload.priority, kind: payload.kind === 'Epic' ? 'Epic' : 'Task', readiness: 'Planned', status: config.statuses.todo, humanInput: payload.human_input === undefined ? 'Not assessed' : required(payload.human_input, 'human_input') }, fields };
}

export async function createPlannedIssue(github, snapshot, config, argument, payload, apply = false) {
  const plan = issuePlan(snapshot, config, argument, payload, github.allIssues(config.repository), config.labels ? github.labels(config.repository) : []);
  if (!apply || plan.action === 'duplicate') return { ...plan, applied: false, notice: plan.action === 'duplicate' ? 'Existing issue preserved. Inspect semantic duplicates and use an explicit reviewed edit for changes or partial-write repair.' : 'Dry run; no GitHub writes.' };
  let issue;
  try {
    issue = github.createIssue(config.repository, plan.issue);
    const itemId = github.addItem(snapshot.project.id, issue.node_id);
    for (const [role, value] of Object.entries(plan.assignments)) github.setField(snapshot.project.id, itemId, plan.fields[role], value);
    for (const dependency of plan.dependencies) github.addDependency(issue.node_id, dependency.node_id);
    return { action: 'created', applied: true, number: issue.number, url: issue.html_url, itemId, fingerprint: plan.fingerprint };
  } catch (error) {
    throw new Error(`Issue write incomplete${issue ? `; repair existing #${issue.number} (${issue.html_url}) instead of creating another issue` : ''}: ${error.message}`);
  }
}

export function transitionPlan(snapshot, config, argument, number, phase, payload = {}) {
  const fields = validateMapping(snapshot, config);
  const target = resolveTarget(snapshot, argument, config);
  const item = target.items.find(i => i.issue.number === number);
  if (!item) throw new Error(`Issue #${number} is not in the live target`);
  const phases = {
    planned: [config.statuses.todo, 'Planned'], ready: [config.statuses.todo, 'Ready'],
    claim: [config.statuses.active, 'In Progress'], review: [config.statuses.active, 'In Review'],
    qa: [config.statuses.active, 'QA'], blocked: [config.statuses.active, 'Blocked'], done: [config.statuses.done, 'Done'],
  };
  if (!phases[phase]) throw new Error(`Unknown phase: ${phase}`);
  if (phase !== 'done' && item.issue.state !== 'OPEN') throw new Error('Only open issues can enter an active phase');
  if (phase === 'done' && (item.issue.state !== 'CLOSED' || item.issue.stateReason !== 'COMPLETED')) throw new Error('Done requires an issue closed as COMPLETED and independently verified acceptance; this helper does not close issues');
  if (['planned', 'ready'].includes(phase) && (item.issue.assignees.length || item.issue.prs.some(p => p.state === 'OPEN') || item.fields[fields.status.name] === config.statuses.active)) throw new Error('Do not reset claimed work; coordinate explicitly with its owner');
  if (phase === 'ready') {
    list(payload.evidence, 'evidence');
    const candidate = { ...item, fields: { ...item.fields, [fields.readiness.name]: 'Ready' } };
    const candidateSnapshot = { ...snapshot, items: snapshot.items.map(i => i.id === item.id ? candidate : i) };
    const eligible = analyzeScope(candidateSnapshot, { ...target, items: target.items.map(i => i.id === item.id ? candidate : i) }, config).readyTasks;
    if (!eligible.some(i => i.id === item.id)) throw new Error('Dependencies, decisions, kind or status prevent Ready');
  }
  if (phase === 'claim') {
    if (!/^[a-z\d](?:[a-z\d-]{0,38})$/i.test(required(payload.owner, 'owner'))) throw new Error('owner must be a GitHub login');
    required(payload.branch, 'branch');
    const branch = payload.branch.replace(/^refs\/heads\//, '');
    if (/[\s\x00-\x1f~^:?*\[\]\\]/.test(branch) || branch.startsWith('-') || branch.startsWith('/') || branch.endsWith('/') || branch.includes('..') || branch.includes('@{') || branch.split('/').some(part => !part || part.startsWith('.') || part.endsWith('.lock') || part.endsWith('.')) || ['main', 'master', snapshot.repository.defaultBranch, '@'].includes(branch)) throw new Error('claim requires a dedicated branch');
    if (!analyzeScope(snapshot, target, config).readyTasks.some(i => i.id === item.id)) throw new Error('Task is no longer eligible to claim');
  }
  if (phase === 'blocked') required(payload.reason, 'reason');
  if (['review', 'qa', 'done'].includes(phase)) list(payload.evidence, 'evidence');
  if (phase === 'review' && !item.issue.prs.some(p => p.state === 'OPEN')) throw new Error('In Review requires an open linked PR');
  if (['review', 'qa'].includes(phase) && item.fields[fields.status.name] !== config.statuses.active) throw new Error('Review/QA requires claimed In Progress work');
  const [status, readiness] = phases[phase];
  return { number, phase, itemId: item.id, fields, assignments: { status, readiness }, payload, expectedUpdatedAt: item.issue.updatedAt };
}

export async function transition(github, snapshot, config, argument, number, phase, payload, apply = false) {
  // Project field edits do not necessarily change issue.updated_at. Re-read decisions,
  // dependencies and ownership immediately before authorizing an applied transition.
  if (apply) snapshot = await github.snapshot(config);
  const plan = transitionPlan(snapshot, config, argument, number, phase, payload);
  if (!apply) return { ...plan, applied: false };
  const current = github.issue(config.repository, number);
  if (current.updated_at !== plan.expectedUpdatedAt || current.state.toUpperCase() !== (phase === 'done' ? 'CLOSED' : 'OPEN') || (phase === 'done' && current.state_reason !== 'completed')) throw new Error('Issue changed since snapshot; refresh and coordinate before retrying');
  if (phase === 'claim' && current.assignees.length) throw new Error('Task was claimed concurrently');
  try {
    if (phase === 'claim') github.updateIssue(config.repository, number, { assignees: [payload.owner] });
    github.comment(config.repository, number, `Engineering workflow: **${phase}**\n\n${phase === 'claim' ? `Owner: @${payload.owner}\nBranch: ${payload.branch}\n\nCoordinator must confirm exclusive ownership before coding.` : phase === 'blocked' ? payload.reason : (payload.evidence || []).join('\n')}`);
    for (const [role, value] of Object.entries(plan.assignments)) github.setField(snapshot.project.id, plan.itemId, plan.fields[role], value);
    return { number, phase, applied: true, notice: 'Re-read live issue/Project to confirm. GitHub writes are not transactional; shared credentials require one coordinator.' };
  } catch (error) {
    throw new Error(`Transition incomplete for #${number}; inspect current issue and Project fields before repair: ${error.message}`);
  }
}
