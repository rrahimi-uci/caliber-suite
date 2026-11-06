import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { GitHub } from '../../.agents/lib/github.mjs';
import { validateMapping } from '../../.agents/lib/model.mjs';

const root = resolve(import.meta.dirname, '../..');
const config = JSON.parse(readFileSync(resolve(root, '.agents/github/project.json'), 'utf8'));
const snapshot = await new GitHub().snapshot(config);
validateMapping(snapshot, config);
process.stdout.write(JSON.stringify(snapshot));
