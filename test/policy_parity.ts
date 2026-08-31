/**
 * Parity: the TypeScript `decide()` in the OpenClaw plugin must agree with the
 * Python `decide()` in meg/policy.py, case for case.
 *
 * Two implementations of one policy is exactly how a gate starts meaning
 * different things in different runtimes — an agent governed by the OpenClaw
 * plugin and a script governed by the Python library must not reach opposite
 * conclusions about the same task. The fixtures live in one JSON file that both
 * sides read, so a case cannot be added to one and forgotten in the other.
 *
 *   npx tsx test/policy_parity.ts
 */
import { readFileSync } from 'node:fs';
import { decide } from '../integrations/openclaw/index.js';

type Json = Record<string, unknown>;

const routes: Json = JSON.parse(readFileSync('routes.json', 'utf8'));
const cases: Array<{
  name: string;
  mode: string;
  meta: Json;
  strict?: boolean;
  expect: { allowed: boolean; reason_contains?: string; action?: string };
}> = JSON.parse(readFileSync('test/policy_cases.json', 'utf8'));

let failed = 0;
for (const c of cases) {
  const d = decide(c.mode, c.meta, routes, c.strict ?? false);
  const okAllowed = d.allowed === c.expect.allowed;
  const okReason =
    !c.expect.reason_contains ||
    d.reason.toLowerCase().includes(c.expect.reason_contains.toLowerCase());
  // The graduated action is checked too: `allowed` alone cannot tell a plain
  // allow from a nudge, and the nudge is the whole point of the middle states.
  const okAction = !c.expect.action || d.action === c.expect.action;
  if (okAllowed && okReason && okAction) {
    console.log(`  ✓ ${c.name}`);
  } else {
    failed++;
    console.log(
      `  ✗ ${c.name}: allowed=${d.allowed} (want ${c.expect.allowed}) action=${d.action} (want ${c.expect.action ?? 'any'})  reason="${d.reason}"`,
    );
  }
}

console.log(failed ? `\n✗ TS policy parity: ${failed} failed` : '\n✓ TS policy parity: all passed');
process.exit(failed ? 1 : 0);
