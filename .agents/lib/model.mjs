/** Pure Project-domain operations. No network, clock, filesystem, or input mutation. */
const FIELD_TYPES = {
  status: 'SINGLE_SELECT', readiness: 'TEXT', release: 'SINGLE_SELECT',
  cycle: 'TEXT', priority: 'TEXT', kind: 'TEXT', humanInput: 'TEXT',
};
const folded = value => String(value ?? '').toLowerCase();
const nonempty = value => typeof value === 'string' && value.trim().length > 0;
const same = (a, b) => folded(a) === folded(b);
const issueKey = issue => `${folded(issue.repository)}#${issue.number}`;
const closed = issue => same(issue.state, 'CLOSED');
const completed = issue => closed(issue) && same(issue.stateReason, 'COMPLETED');
const PRIORITIES = ['P0', 'P1', 'P2', 'P3'];

/** Return live field objects keyed by configuration role, retaining live IDs/options. */
export function validateMapping(snapshot, config) {
  if (!nonempty(config?.repository) || !same(snapshot?.repository?.nameWithOwner, config.repository)) {
    throw new Error('Snapshot repository does not match configuration');
  }
  if (!Array.isArray(snapshot.fields)) throw new Error('Snapshot fields are missing');
  const mapping = {};
  const ids = new Set();
  for (const [role, type] of Object.entries(FIELD_TYPES)) {
    const name = config.fields?.[role];
    if (!nonempty(name)) throw new Error(`Missing configured field: ${role}`);
    const matches = snapshot.fields.filter(field => field.name === name);
    if (matches.length !== 1) throw new Error(`Missing or ambiguous live field: ${name}`);
    const field = matches[0];
    if (!nonempty(field.id) || ids.has(field.id)) throw new Error(`Missing or reused field ID: ${name}`);
    if (field.dataType !== type) throw new Error(`Field type drift: ${name} must be ${type}, got ${field.dataType}`);
    ids.add(field.id);
    if (type === 'SINGLE_SELECT') {
      if (!Array.isArray(field.options)) throw new Error(`Missing options for ${name}`);
      const optionIds = new Set();
      const names = new Set();
      for (const option of field.options) {
        if (!nonempty(option.id) || !nonempty(option.name) || optionIds.has(option.id) || names.has(option.name)) {
          throw new Error(`Invalid or duplicate options for ${name}`);
        }
        optionIds.add(option.id);
        names.add(option.name);
      }
    }
    mapping[role] = field;
  }
  const statuses = ['todo', 'active', 'done'].map(role => config.statuses?.[role]);
  if (new Set(statuses).size !== 3) throw new Error('Configured statuses must be distinct');
  for (const name of statuses) {
    if (!nonempty(name) || !mapping.status.options.some(option => option.name === name)) {
      throw new Error(`Missing configured status option: ${name}`);
    }
  }
  return mapping;
}

function liveItems(snapshot, config) {
  return snapshot.items.filter(item => !item.isArchived && item.issue && same(item.issue.repository, config.repository));
}

/** Resolve a live exact name; qualification disambiguates domains, # disambiguates milestones. */
export function resolveTarget(snapshot, argument, config) {
  const mapping = validateMapping(snapshot, config);
  if (!nonempty(argument)) throw new Error('Target must not be empty');
  const raw = argument.trim();
  const qualified = /^(release|cycle|milestone):(.*)$/i.exec(raw);
  const kind = qualified ? folded(qualified[1]) : null;
  const name = qualified ? qualified[2].trim() : raw;
  if (!name) throw new Error('Target must not be empty');
  const items = liveItems(snapshot, config);
  const candidates = [
    ...mapping.release.options.map(option => ({ kind: 'release', name: option.name })),
    ...[...new Set(items.map(item => item.fields[mapping.cycle.name]).filter(nonempty))]
      .map(name => ({ kind: 'cycle', name })),
    ...snapshot.milestones.map(milestone => ({ kind: 'milestone', name: milestone.title, milestone })),
  ].filter(candidate => (!kind || candidate.kind === kind) && (
    kind === 'milestone' && /^#\d+$/.test(name)
      ? candidate.milestone.number === Number(name.slice(1))
      : same(candidate.name, name)
  ));
  if (!candidates.length) throw new Error(`Unknown live target: ${raw}`);
  if (candidates.length !== 1) throw new Error(`Ambiguous live target: ${raw}; qualify release:, cycle:, or milestone:#number`);
  const target = candidates[0];
  const selected = items.filter(item => target.kind === 'milestone'
    ? item.issue.milestone?.number === target.milestone.number
    : item.fields[mapping[target.kind].name] === target.name);
  if (!selected.length) throw new Error(`Empty live target: ${raw}`);
  const milestones = target.kind === 'milestone' ? [target.milestone] :
    [...new Map(selected.filter(item => item.issue.milestone)
      .map(item => [item.issue.milestone.number, item.issue.milestone])).values()];
  return {
    kind: target.kind, name: target.name,
    key: `${target.kind}:${target.kind === 'milestone' ? `#${target.milestone.number}` : target.name}`,
    items: selected, milestones,
  };
}

function control(item, mapping) {
  return item.issue.labels.some(label => same(label, 'milestone-control')) ||
    /milestone control/i.test(item.issue.title) || ['control', 'gate'].includes(folded(item.fields[mapping.kind.name]));
}

function task(item, mapping) {
  const labels = item.issue.labels.map(folded);
  const kind = folded(item.fields[mapping.kind.name]);
  return !control(item, mapping) && !['epic', 'coordination'].includes(kind) &&
    !labels.some(label => ['epic', 'coordination', 'human-input'].includes(label)) &&
    (kind === 'task' || labels.includes('task'));
}

// A claim is evidence of existing work even if readiness or dependencies drift later.
// The adapter's PR connection only includes closing references. Complete-task PRs
// must use a closing reference (e.g. "Closes #123"); "Refs #123" alone is not
// discoverable here. Keep assignee/status claims for all ongoing work and slices.
function claimed(item, mapping, config) {
  return item.issue.assignees.length > 0 ||
    same(item.fields[mapping.status.name], config.statuses.active) ||
    item.issue.prs.some(pr => same(pr.state, 'OPEN'));
}

function reachesCycle(key, graph, visiting = new Set(), visited = new Set()) {
  if (visiting.has(key)) return true;
  if (visited.has(key)) return false;
  visiting.add(key);
  for (const dependency of graph.get(key) || []) {
    if (reachesCycle(dependency, graph, visiting, visited)) return true;
  }
  visiting.delete(key);
  visited.add(key);
  return false;
}

// Conservative ownership roots: globs own their complete static directory prefix.
// Unknown/invalid ownership serializes the task; it never proves independence.
function ownership(item) {
  const markers = [...item.issue.body.matchAll(/<!--\s*caliber-engineering-meta:(.*?)\s*-->/gs)];
  if (markers.length !== 1) return null;
  let meta;
  try { meta = JSON.parse(markers[0][1]); } catch { return null; }
  if (!Array.isArray(meta?.owned_paths) || !meta.owned_paths.length) return null;
  const roots = [];
  for (const path of meta.owned_paths) {
    if (!nonempty(path) || path !== path.trim() || path.startsWith('/') ||
        /[\\:\x00-\x1f]/.test(path) || path.includes('//') || path.split('/').some(part => ['..', '.'].includes(part))) return null;
    const wildcard = path.search(/[*?\[\]{}!]/);
    const prefix = wildcard < 0 ? path : path.slice(0, wildcard);
    const root = wildcard < 0 ? prefix : prefix.slice(0, prefix.lastIndexOf('/') + 1);
    // Serialize case aliases as well: contributor worktrees may be case insensitive.
    roots.push(folded(root.replace(/\/+$/, '')));
  }
  return roots;
}

function overlaps(left, right) {
  return !left || !right || left === right || left.startsWith(`${right}/`) || right.startsWith(`${left}/`);
}

function batches(tasks) {
  const result = [];
  for (const item of tasks) {
    const roots = ownership(item);
    const batch = roots && result.find(batch => batch.length < 4 && batch.every(other => {
      const others = ownership(other);
      return others && !roots.some(a => others.some(b => overlaps(a, b)));
    }));
    if (batch) batch.push(item);
    else result.push([item]);
  }
  return result;
}

/**
 * Classify scope without executing work. Arrays contain original snapshot items.
 * Blockers are {item, code, reason}; item is null for a missing acceptance control.
 * Closure is dependency state only, never proof of release acceptance. Controls
 * always require a separate evidence audit, which this pure snapshot cannot do.
 * Uninspectable items block scope acceptance, not independent task development.
 */
export function analyzeScope(snapshot, target, config) {
  const mapping = validateMapping(snapshot, config);
  const ids = new Set(target.items.map(item => item.id));
  const items = liveItems(snapshot, config).filter(item => ids.has(item.id));
  if (!items.length) throw new Error('Cannot analyze an empty live scope');
  const known = new Map(snapshot.items.filter(item => item.issue).map(item => [issueKey(item.issue), item.issue]));
  const graph = new Map([...known].map(([key, issue]) => [key, issue.blockedBy.map(issueKey)]));
  const readyTasks = [], claimedTasks = [], blockers = [];
  const add = (item, code, reason) => blockers.push({ item, code, reason });
  const uninspectableItems = snapshot.items.filter(item => {
    if (item.isArchived || item.issue) return false;
    const release = item.fields?.[mapping.release.name];
    // A hidden issue has no inspectable repository or milestone identity. Only
    // an explicit different release can exclude it from a release target.
    return target.kind !== 'release' || !nonempty(release) || same(release, target.name);
  });
  for (const item of uninspectableItems) {
    add(item, 'scope-uninspectable', `Project item ${item.id} has no inspectable issue; resolve its scope and acceptance requirements before declaring the target ready`);
  }
  const controls = items.filter(item => control(item, mapping));
  if (!controls.length) add(null, 'acceptance-control-missing', 'No milestone acceptance control in scope; issue closure cannot prove acceptance');
  for (const item of controls) add(item, 'acceptance-evidence-required', 'Acceptance needs current candidate evidence; control issue state alone is insufficient');
  const openItems = items.filter(item => !closed(item.issue));
  for (const item of openItems) {
    if (!task(item, mapping)) {
      if (!control(item, mapping)) add(item, 'not-actionable-task', 'Requires an explicit Task kind or task label and must not be epic/coordination work');
      continue;
    }
    const occupied = claimed(item, mapping, config);
    if (occupied) claimedTasks.push(item);
    const reasons = [];
    if (!same(item.issue.state, 'OPEN')) reasons.push(['unknown-state', 'Issue state is not OPEN']);
    if (occupied) reasons.push(['claimed', 'Task has an assignee, active status, or open PR']);
    if (!same(item.fields[mapping.status.name], config.statuses.todo)) reasons.push(['status-not-todo', 'Task status must be the configured Todo status']);
    if (!same(item.fields[mapping.readiness.name], 'Ready')) reasons.push(['not-ready', 'Readiness must be Ready']);
    if (!PRIORITIES.includes(item.fields[mapping.priority.name])) reasons.push(['priority-unclassified', 'Planner must classify priority as P0, P1, P2, or P3 before selection']);
    const human = String(item.fields[mapping.humanInput.name] ?? '').trim();
    const acceptanceOnly = human.split(',').every(segment => /^H\d+: acceptance$/.test(segment.trim()));
    if (acceptanceOnly) {
      add(item, 'acceptance-human-input', `Development may proceed; acceptance still requires evidence for ${human}`);
    } else if (human && !['none', 'no', 'n/a', 'not required', 'none pending'].includes(folded(human))) {
      reasons.push(['human-input', 'Human input is unresolved or requires coordination']);
    }
    for (const dependency of item.issue.blockedBy) {
      const tracked = known.get(issueKey(dependency));
      if (!completed(dependency) || (tracked && !completed(tracked))) {
        reasons.push(['native-blocker', `Unresolved native blocker ${issueKey(dependency)}; requires CLOSED with stateReason COMPLETED`]);
      }
    }
    if (reachesCycle(issueKey(item.issue), graph)) reasons.push(['dependency-cycle', 'Task reaches a cycle in native dependencies']);
    for (const [code, reason] of reasons) add(item, code, reason);
    if (!reasons.length) readyTasks.push(item);
  }
  readyTasks.sort((a, b) => PRIORITIES.indexOf(a.fields[mapping.priority.name]) -
    PRIORITIES.indexOf(b.fields[mapping.priority.name]) || a.issue.number - b.issue.number);
  return { readyTasks, claimedTasks, blockers, openItems, uninspectableItems, parallelBatches: batches(readyTasks) };
}
