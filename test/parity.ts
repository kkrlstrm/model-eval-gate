/**
 * TS half of the offline test suite. Asserts:
 *  - schema validation rejects the classic bad policies (generic name, bad date,
 *    unknown key, modes/retired overlap) and accepts the real routes.json
 *  - the provider-pin wire mapping matches the shared fixtures
 *  - constraint verdicts match the shared fixtures
 *  - the allowlist and refusal behavior match routes.json
 * No network, no API key. Run with: npx tsx test/parity.ts
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { validateRoutes } from '../src/schema.ts';
import { providerToHttp } from '../src/wire.ts';
import { canDelegate } from '../src/preflight.ts';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const fx = JSON.parse(readFileSync(join(HERE, 'fixtures.json'), 'utf8'));
const routes = JSON.parse(readFileSync(join(ROOT, 'routes.json'), 'utf8'));

let failed = 0;
const eq = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
function ok(name: string, cond: boolean, detail = '') {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    console.error(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
    failed++;
  }
}

console.log('schema validation');
ok('real routes.json validates', validateRoutes(routes).ok);
ok('generic mode name rejected', !validateRoutes({ modes: { chat: mode() } }).ok);
ok(
  'bad verified_date rejected',
  !validateRoutes({ modes: { 'extract-x': mode({ verified_date: 'nope' }) } }).ok,
);
ok('unknown key rejected', !validateRoutes({ modes: { 'extract-x': { ...mode(), typo: 1 } } }).ok);
ok(
  'modes/retired overlap rejected',
  !validateRoutes({ modes: { 'extract-x': mode() }, retired: { 'extract-x': { reason: 'r' } } }).ok,
);
ok(
  'missing required field rejected',
  !validateRoutes({ modes: { 'extract-x': { model: 'm' } } }).ok,
);

console.log('provider-pin wire mapping');
for (const c of fx.provider_pins) {
  ok(
    `pin ${JSON.stringify(c.pin)}`,
    eq(providerToHttp(c.pin), c.http),
    `got ${JSON.stringify(providerToHttp(c.pin))}`,
  );
}

console.log('constraint verdicts');
for (const c of fx.constraint_cases) {
  const got = canDelegate({ constraints: c.constraints } as any, c.meta).ok;
  ok(
    `${JSON.stringify(c.constraints)} vs ${JSON.stringify(c.meta)} → ${c.ok}`,
    got === c.ok,
    `got ${got}`,
  );
}

console.log('allowlist + refusals');
for (const m of fx.refusals.must_exist) ok(`mode "${m}" present`, m in routes.modes);
for (const m of fx.refusals.unknown) ok(`mode "${m}" absent (would refuse)`, !(m in routes.modes));

function mode(over: Record<string, unknown> = {}) {
  return {
    model: 'x',
    priceIn: 0,
    priceOut: 0,
    purpose: 'p',
    use_when: 'u',
    do_not_use_when: 'd',
    verified_date: '2026-01-01',
    ...over,
  };
}

console.log(failed ? `\n✗ TS parity: ${failed} failure(s)` : '\n✓ TS parity: all passed');
process.exit(failed ? 1 : 0);
