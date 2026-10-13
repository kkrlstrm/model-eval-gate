/**
 * Parity: src/schema.ts must reach the same verdict as meg/routes_schema.py on every
 * policy file in test/routes_cases.json (test/test_pipeline.py runs the Python half,
 * and RunTune runs the same file through its policy loader). One case file, so a rule
 * cannot be added to one validator and forgotten in another.
 *
 *   npx tsx test/routes_parity.ts
 */
import { readFileSync } from 'node:fs';
import { validateRoutes } from '../src/schema.ts';

const cases: { name: string; routes: unknown; valid: boolean; error_contains?: string }[] = JSON.parse(
  readFileSync('test/routes_cases.json', 'utf8'),
);
let failed = 0;
for (const c of cases) {
  const r = validateRoutes(c.routes);
  const errors = r.ok ? [] : r.errors;
  const verdictOk = r.ok === c.valid;
  const named = !c.error_contains || errors.some((e) => e.includes(c.error_contains!));
  if (verdictOk && named) console.log(`  ✓ ${c.name}`);
  else {
    failed++;
    console.log(`  ✗ ${c.name}: valid=${r.ok} (want ${c.valid})${c.error_contains ? `, want an error naming "${c.error_contains}"` : ''} — ${errors.join(' | ')}`);
  }
}
console.log(failed ? `\n✗ routes schema parity: ${failed} failed` : '\n✓ routes schema parity: all passed');
process.exit(failed ? 1 : 0);
