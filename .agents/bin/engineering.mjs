#!/usr/bin/env node
import { run } from '../lib/cli.mjs';
try {
  const result = await run(process.argv.slice(2));
  process.stdout.write(`${typeof result === 'string' ? result : JSON.stringify(result, null, 2)}\n`);
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}
