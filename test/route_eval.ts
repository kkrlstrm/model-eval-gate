/**
 * Offline tests for eval/route_eval.ts — the verdict rules and the document RunTune
 * reads. No network. The pass case is also written to test/route_eval_golden.json,
 * which test/contract_runtune.py checks with RunTune's own validator, so a field
 * renamed on either side fails a test instead of a production apply.
 *
 *   npx tsx test/route_eval.ts            # check
 *   UPDATE_GOLDEN=1 npx tsx test/route_eval.ts
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { SuiteResult } from '../eval/harness.ts';
import { buildRouteEval, MIN_CASES } from '../eval/route_eval.ts';

const HERE = dirname(fileURLToPath(import.meta.url));
let failed = 0;
const ok = (name: string, cond: boolean, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${!cond && detail ? ` — ${detail}` : ''}`);
  if (!cond) failed++;
};

function suite(
  model: string,
  score: number,
  opts: { n?: number; passHatK?: number; unavailable?: number; ids?: string[] } = {},
): SuiteResult {
  const n = opts.n ?? 24;
  const ids = opts.ids ?? Array.from({ length: n }, (_, i) => `t-${String(i).padStart(2, '0')}`);
  const k = 3;
  return {
    model,
    label: model,
    k,
    threshold: 0.66,
    tasks: ids.map((id) => ({
      task_id: id,
      trials: Array.from({ length: k }, () => ({
        pass: true,
        score,
        ms: 10,
        cost: 0.0001,
        costReported: true,
        provider: 'p',
        graders: [],
        parseOk: true,
        outcome: 'ok' as const,
      })),
      passes: k,
      passAtK: 1,
      passHatK: 1,
      meanScore: score,
      scoreStdev: 0,
    })),
    passAtK: 1,
    passHatK: opts.passHatK ?? 0.9,
    meanScore: score,
    totalCost: 0.0072,
    totalCostReported: true,
    totalMs: 1000,
    providersSeen: ['p'],
    outcomes: {
      ok: ids.length * k - (opts.unavailable ?? 0),
      provider_unavailable: opts.unavailable ?? 0,
      parse_failure: 0,
      grader_failure: 0,
    },
  };
}
const spec = {
  graders: [{ kind: 'fieldAgreement', name: 'agreement' }],
  consistency_floor: 0.5,
  drift_tolerance: 0.1,
  baseline: { meanScore: 0.9 },
};
const now = new Date('2026-10-13T12:00:00Z');
const base = { mode: 'extract-fields', spec, harness: 'model-eval-gate eval/bakeoff.ts', now };

console.log('verdict rules');
const pass = buildRouteEval({
  ...base,
  candidate: suite('cand/a', 0.91),
  incumbent: suite('inc/b', 0.9),
  margin: 0.02,
});
ok(
  'candidate at least as good as the incumbent passes',
  pass.verdict === 'pass',
  pass.reasons.join('; '),
);
const within = buildRouteEval({
  ...base,
  candidate: suite('cand/a', 0.89),
  incumbent: suite('inc/b', 0.9),
  margin: 0.02,
});
ok('inside the declared margin passes', within.verdict === 'pass');
const below = buildRouteEval({
  ...base,
  candidate: suite('cand/a', 0.85),
  incumbent: suite('inc/b', 0.9),
  margin: 0.02,
});
ok('below the incumbent by more than the margin fails', below.verdict === 'fail');
const noMargin = buildRouteEval({
  ...base,
  candidate: suite('cand/a', 0.899),
  incumbent: suite('inc/b', 0.9),
});
ok('with no margin, any shortfall fails', noMargin.verdict === 'fail');
const shaky = buildRouteEval({
  ...base,
  candidate: suite('cand/a', 0.95, { passHatK: 0.3 }),
  incumbent: suite('inc/b', 0.9),
});
ok('below the consistency floor fails', shaky.verdict === 'fail');
const thin = buildRouteEval({
  ...base,
  candidate: suite('cand/a', 0.95, { n: MIN_CASES - 1 }),
  incumbent: suite('inc/b', 0.9, { n: MIN_CASES - 1 }),
});
ok(`fewer than ${MIN_CASES} cases cannot pass`, thin.verdict === 'fail');
const down = buildRouteEval({
  ...base,
  candidate: suite('cand/a', 0.95, { unavailable: 30 }),
  incumbent: suite('inc/b', 0.9),
});
ok('an arm that mostly never reached a provider is inconclusive', down.verdict === 'inconclusive');
const reclear = buildRouteEval({ ...base, candidate: suite('inc/b', 0.85) });
ok(
  're-clearing: within baseline - tolerance passes',
  reclear.verdict === 'pass' && reclear.incumbent === undefined,
);
const regress = buildRouteEval({ ...base, candidate: suite('inc/b', 0.7) });
ok('re-clearing: below baseline - tolerance fails', regress.verdict === 'fail');
let threw = false;
try {
  buildRouteEval({
    ...base,
    candidate: suite('cand/a', 0.9),
    incumbent: suite('inc/b', 0.9, { ids: ['x'] }),
  });
} catch {
  threw = true;
}
ok('arms scored on different tasks refuse to compare', threw);

console.log('document');
for (const f of [
  'schema',
  'mode',
  'candidate',
  'incumbent',
  'verdict',
  'cases',
  'metric',
  'candidate_score',
  'incumbent_score',
  'non_inferiority_margin',
  'created',
  'harness',
])
  ok(`carries ${f}`, (pass as any)[f] !== undefined);
ok('schema is runtune.route-eval/1', pass.schema === 'runtune.route-eval/1');

const goldenPath = join(HERE, 'route_eval_golden.json');
const text = JSON.stringify(pass, null, 2) + '\n';
if (process.env.UPDATE_GOLDEN) {
  writeFileSync(goldenPath, text);
  console.log(`  ↳ wrote ${goldenPath}`);
} else {
  ok(
    'matches test/route_eval_golden.json (UPDATE_GOLDEN=1 to refresh)',
    readFileSync(goldenPath, 'utf8') === text,
  );
}

console.log(failed ? `\n✗ route-eval: ${failed} failure(s)` : '\n✓ route-eval: all passed');
process.exit(failed ? 1 : 0);
