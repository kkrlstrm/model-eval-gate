/**
 * Regression runner — the "does it STILL work?" half of eval-gated routing.
 *
 * Each capability eval that cleared the bar graduates into a frozen regression
 * spec (eval/regression/*.json): a task set + gold + a recorded baseline. This
 * runner re-runs each spec against the model the ALLOWLIST currently routes that
 * mode to (resolved live from routes.json, so a swapped model is caught), over k
 * TRIALS, and flags drift:
 *
 *   • score drift   — mean grader score fell > drift_tolerance below baseline
 *   • consistency   — pass^k (all k trials pass) fell below consistency_floor
 *   • model swap    — routes.json now points the mode at a different model than
 *                     the baseline was recorded on
 *
 * On drift it exits non-zero (wire that into CI or a scheduled job). This turns a
 * one-time eval verdict into a maintained guarantee.
 *
 * Usage:
 *   npx tsx eval/regression.ts                 # run every spec
 *   npx tsx eval/regression.ts --mode filter-auto-reply
 *   npx tsx eval/regression.ts --spec eval/regression/auto-reply.json --k 5
 *   npx tsx eval/regression.ts --update-baseline   # record current numbers as the new baseline
 */
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { OpenRouter } from '@openrouter/sdk';
import { runSuite, type ModelSpec, type Task } from './harness.ts';
import { buildGrader } from './graders.ts';
import { loadRoutesOrExit, validateSpec } from '../src/schema.ts';

const HERE = dirname(fileURLToPath(import.meta.url)); // eval/
const ROUTES = loadRoutesOrExit(join(HERE, '..', 'routes.json')); // validated, fail-closed

// ── args ──
const argv = process.argv.slice(2);
const flag = (name: string): string | undefined => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
};
const has = (name: string) => argv.includes(name);
const kOverride = flag('--k') ? Number(flag('--k')) : undefined;
const onlyMode = flag('--mode');
const onlySpec = flag('--spec');
const updateBaseline = has('--update-baseline');

// ── which specs ──
const specDir = join(HERE, 'regression');
const specFiles = onlySpec
  ? [onlySpec.startsWith('/') ? onlySpec : join(process.cwd(), onlySpec)]
  : readdirSync(specDir)
      .filter((f) => f.endsWith('.json'))
      .map((f) => join(specDir, f));

function parseJsonObject(text: string): any {
  const s = (text ?? '')
    .trim()
    .replace(/^```(?:json)?\s*/i, '')
    .replace(/\s*```$/, '');
  const m = s.match(/\{[\s\S]*\}/);
  if (!m) return null;
  try {
    return JSON.parse(m[0]);
  } catch {
    return null;
  }
}

function render(tpl: string, vars: Record<string, any>): string {
  return tpl.replace(/\{\{(\w+)\}\}/g, (_, k) => String(vars?.[k] ?? ''));
}

const client = new OpenRouter({ apiKey: process.env.OPENROUTER_API_KEY });
if (!process.env.OPENROUTER_API_KEY) {
  console.error('✗ OPENROUTER_API_KEY not set — regression needs live model calls.');
  process.exit(3);
}

const today = new Date().toISOString().slice(0, 10);
const allResults: any[] = [];
let anyDrift = false;

for (const specPath of specFiles) {
  const rawSpec = JSON.parse(readFileSync(specPath, 'utf8'));
  const sv = validateSpec(rawSpec);
  if (!sv.ok) {
    console.error(`✗ ${specPath}: invalid regression spec — ${sv.errors.join('; ')}`);
    anyDrift = true;
    continue;
  }
  const spec: any = rawSpec;
  if (onlyMode && spec.mode !== onlyMode) continue;

  const routeMode = ROUTES.modes?.[spec.mode];
  if (!routeMode) {
    console.error(`✗ ${spec.mode}: not on the allowlist (routes.json) — skipping.`);
    anyDrift = true;
    continue;
  }

  const k = kOverride ?? spec.k ?? 3;
  const modelSpec: ModelSpec = {
    model: routeMode.model,
    label: `${spec.mode} (${routeMode.model})`,
    priceIn: routeMode.priceIn ?? 0,
    priceOut: routeMode.priceOut ?? 0,
    provider: routeMode.provider ?? null,
  };
  const tasks: Task[] = spec.tasks.map((t: any) => ({
    id: t.id,
    input: render(spec.input_template, t.vars),
    gold: t.gold,
  }));
  const graders = spec.graders.map(buildGrader);

  console.log(`\n══ ${spec.mode} · ${spec.title} ══`);
  console.log(
    `   model=${modelSpec.model}  k=${k}  tasks=${tasks.length}${modelSpec.provider ? `  pin=${JSON.stringify(modelSpec.provider)}` : ''}`,
  );

  const suite = await runSuite({
    client,
    spec: modelSpec,
    tasks,
    parse: parseJsonObject,
    graders,
    k,
    threshold: spec.threshold ?? 0.66,
    onProgress: () => process.stdout.write('.'),
  });
  process.stdout.write('\n');

  // ── signals with an error taxonomy ──
  // Separate infra failure from model quality: a provider outage or a parse/grader
  // failure is NOT a model regression, and low scores from an inconclusive run are
  // meaningless, so quality drift is only judged when the run actually reached the model.
  const totalTrials = k * tasks.length;
  const oc = suite.outcomes;
  const inconclusive = oc.provider_unavailable / totalTrials > 0.2;

  const base = spec.baseline ?? {};
  const signals: { type: string; detail: string }[] = [];
  if (inconclusive)
    signals.push({
      type: 'provider_unavailable',
      detail: `${oc.provider_unavailable}/${totalTrials} trials never reached a provider — run inconclusive, NOT scored as model drift`,
    });
  if (oc.parse_failure / totalTrials > 0.2)
    signals.push({
      type: 'parse_failure',
      detail: `${oc.parse_failure}/${totalTrials} trials returned unparseable output`,
    });
  if (oc.grader_failure)
    signals.push({
      type: 'grader_failure',
      detail: `${oc.grader_failure} trial(s) threw inside a grader`,
    });
  if (base.model && base.model !== modelSpec.model)
    signals.push({
      type: 'model_swap',
      detail: `baseline on ${base.model}, allowlist now routes to ${modelSpec.model}`,
    });
  if (!inconclusive) {
    if (base.meanScore != null && suite.meanScore < base.meanScore - (spec.drift_tolerance ?? 0.1))
      signals.push({
        type: 'model_quality_drift',
        detail: `meanScore ${suite.meanScore.toFixed(3)} < baseline ${base.meanScore} − tol ${spec.drift_tolerance ?? 0.1}`,
      });
    if (spec.consistency_floor != null && suite.passHatK < spec.consistency_floor)
      signals.push({
        type: 'consistency',
        detail: `pass^k ${suite.passHatK.toFixed(2)} < floor ${spec.consistency_floor}`,
      });
  }

  const costStr = `$${suite.totalCost.toFixed(4)}${suite.totalCostReported ? '' : ' (est)'}`;
  console.log(
    `   pass@k=${suite.passAtK.toFixed(2)}  pass^k=${suite.passHatK.toFixed(2)}  meanScore=${suite.meanScore.toFixed(3)}  cost=${costStr}  ${(suite.totalMs / 1000).toFixed(0)}s`,
  );
  console.log(
    `   outcomes: ok=${oc.ok} provider_unavailable=${oc.provider_unavailable} parse_failure=${oc.parse_failure} grader_failure=${oc.grader_failure}`,
  );
  console.log(
    `   providers served: ${suite.providersSeen.length ? suite.providersSeen.join(', ') : '(unreported)'}`,
  );
  if (base.meanScore != null)
    console.log(
      `   baseline: meanScore=${base.meanScore}  pass^k=${base.passHatK ?? '—'}  on ${base.model ?? '—'} (${base.verified_date ?? 'unrecorded'})`,
    );

  if (signals.length) {
    anyDrift = true;
    console.log(inconclusive ? '   ⚠ INCONCLUSIVE / signals:' : '   ⚠ signals:');
    for (const s of signals) console.log(`     - [${s.type}] ${s.detail}`);
  } else {
    console.log('   ✓ within baseline');
  }

  // ── bootstrap / refresh the baseline on request — only from a clean run ──
  if (updateBaseline) {
    if (oc.ok < totalTrials * 0.8) {
      console.log(
        '   ↳ baseline NOT updated — too many non-ok trials this run (would poison the baseline)',
      );
    } else {
      spec.baseline = {
        meanScore: Number(suite.meanScore.toFixed(3)),
        passHatK: Number(suite.passHatK.toFixed(2)),
        model: modelSpec.model,
        verified_date: today,
      };
      writeFileSync(specPath, JSON.stringify(spec, null, 2) + '\n');
      console.log(`   ↳ baseline updated in ${specPath}`);
    }
  }

  // Per-task detail for any task that wasn't a clean sweep — surfaces *what*
  // drifted (which labels the model got wrong), not just that the aggregate fell.
  const weakTasks = suite.tasks
    .filter((t) => t.meanScore < 1)
    .map((t) => {
      const worst = t.trials.reduce((a, b) => (b.score < a.score ? b : a), t.trials[0]);
      const misses = (worst?.graders ?? []).flatMap((g) =>
        g.assertions.filter((a) => !a.pass).map((a) => `${a.name}: ${a.detail ?? 'miss'}`),
      );
      return {
        id: t.task_id,
        meanScore: Number(t.meanScore.toFixed(3)),
        passHatK: t.passHatK,
        misses,
      };
    });
  allResults.push({
    mode: spec.mode,
    model: modelSpec.model,
    passAtK: suite.passAtK,
    passHatK: suite.passHatK,
    meanScore: suite.meanScore,
    totalCost: suite.totalCost,
    totalCostReported: suite.totalCostReported,
    outcomes: suite.outcomes,
    providersSeen: suite.providersSeen,
    baseline: spec.baseline ?? {},
    signals,
    inconclusive,
    weakTasks,
  });
}

const outPath = join(HERE, 'regression-results.json');
writeFileSync(
  outPath,
  JSON.stringify({ ran_at: new Date().toISOString(), results: allResults }, null, 2) + '\n',
);
console.log(`\nwrote ${outPath}`);
console.log(
  anyDrift
    ? '\n✗ regression: signals detected (drift and/or inconclusive — see above).'
    : '\n✓ regression: all specs within baseline.',
);
process.exit(anyDrift ? 1 : 0);
