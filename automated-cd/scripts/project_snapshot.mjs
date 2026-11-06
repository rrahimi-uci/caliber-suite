import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { GitHub } from '../../.agents/lib/github.mjs';
import { validateMapping } from '../../.agents/lib/model.mjs';

const root = resolve(import.meta.dirname, '../..');
const config = JSON.parse(readFileSync(resolve(root, '.agents/github/project.json'), 'utf8'));
const viewNumber = Number(process.argv[2] ?? 6);
if (!Number.isSafeInteger(viewNumber) || viewNumber < 1) throw new Error('A positive Project view number is required');
const github = new GitHub();
const snapshot = await github.snapshot(config);
validateMapping(snapshot, config);
const view = github.graphql('query($id:ID!,$number:Int!){node(id:$id){... on ProjectV2{view(number:$number){id number name filter}}}}', { id: snapshot.project.id, number: viewNumber }).node?.view;
if (!view || view.number !== viewNumber) throw new Error(`Project view ${viewNumber} is unavailable`);
snapshot.view = view;
process.stdout.write(JSON.stringify(snapshot));
