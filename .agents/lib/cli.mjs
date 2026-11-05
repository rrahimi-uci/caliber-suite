import { readFileSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { GitHub } from './github.mjs';
import { validateMapping } from './model.mjs';
import { context, createPlannedIssue, transition } from './operations.mjs';

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
export const USAGE = `Repository engineering helper (Node >=20, authenticated gh)
  node .agents/bin/engineering.mjs schema
  node .agents/bin/engineering.mjs context <architect|plan|develop|review|critique|bug-bash|release-check> <target>
  node .agents/bin/engineering.mjs issue <target> --file <json> [--apply]
  node .agents/bin/engineering.mjs transition <target> <issue-number> <planned|ready|claim|review|qa|blocked|done> [--file <json>] [--apply]
Quote targets containing spaces. All commands read the live Project first. Writes default to dry-run.
This helper supplies context/validated writes; Codex or Claude executes the canonical workflow.`;

export function parse(args) {
  if (!args.length || ['--help', '-h', 'help'].includes(args[0])) return { help: true };
  const positional = [], options = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--apply') { if (options.apply) throw new Error('Duplicate --apply'); options.apply = true; }
    else if (args[i] === '--file') {
      if (options.file || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('--file requires one JSON filename');
      options.file = args[++i];
    } else if (args[i].startsWith('--')) throw new Error(`Unknown option: ${args[i]}`);
    else positional.push(args[i]);
  }
  const [operation, ...rest] = positional;
  const arity = { schema: 0, context: 2, issue: 1, transition: 3 };
  if (!(operation in arity) || rest.length !== arity[operation]) throw new Error(USAGE);
  if (['schema', 'context'].includes(operation) && (options.file || options.apply)) throw new Error('Read-only commands do not accept write options');
  if (operation === 'issue' && !options.file) throw new Error('issue requires --file');
  if (operation === 'transition' && !/^[1-9]\d*$/.test(rest[1])) throw new Error('Issue number must be a positive integer');
  return { operation, rest, ...options };
}

export function lock(root = ROOT) {
  const common = execFileSync('git', ['rev-parse', '--git-common-dir'], { cwd: root, encoding: 'utf8' }).trim();
  const path = resolve(root, common, 'caliber-engineering-write.lock');
  try { mkdirSync(path); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error(`Another local coordinator holds ${path}; after a crash inspect active processes and remote partial writes before manually removing the stale lock`);
    throw error;
  }
  return () => rmSync(path, { recursive: true });
}

export async function run(args, dependencies = {}) {
  const parsed = parse(args);
  if (parsed.help) return USAGE;
  const github = dependencies.github || new GitHub();
  const read = dependencies.read || (path => JSON.parse(readFileSync(path, 'utf8')));
  const config = dependencies.config || read(resolve(ROOT, '.agents/github/project.json'));
  // Lock all apply calls in a shared git directory, including sibling worktrees.
  // This is local serialization, not a distributed/atomic GitHub claim.
  const release = parsed.apply ? (dependencies.lock || lock)() : () => {};
  try {
    const snapshot = await github.snapshot(config);
    validateMapping(snapshot, config);
    if (parsed.operation === 'schema') return { project: snapshot.project, fields: snapshot.fields, mapping: config, capturedAt: snapshot.capturedAt };
    if (parsed.operation === 'context') return context(snapshot, config, ...parsed.rest);
    const payload = parsed.file ? read(parsed.file) : {};
    if (!payload || Array.isArray(payload) || typeof payload !== 'object') throw new Error('Payload must be a JSON object');
    if (parsed.operation === 'issue') return await createPlannedIssue(github, snapshot, config, parsed.rest[0], payload, !!parsed.apply);
    return await transition(github, snapshot, config, parsed.rest[0], Number(parsed.rest[1]), parsed.rest[2], payload, !!parsed.apply);
  } finally { release(); }
}
