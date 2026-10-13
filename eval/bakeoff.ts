/**
 * Bake-off — score a candidate model against the incumbent on one regression spec,
 * and write the route-eval result a route change carries into RunTune.
 *
 * Both arms run the SAME tasks with the SAME graders and k; that is what makes the
 * comparison an eval of this change rather than two unrelated numbers. The verdict
 * rules live in eval/route_eval.ts.
 *
 *   npx tsx eval/bakeoff.ts --spec eval/regression/extract-fields.json \
 *       --candidate qwen/qwen3-235b-a22b-2507 --incumbent deepseek/deepseek-v4-flash \
 *       --k 3 --margin 0.02 --out route-eval.json
 *
 *   # re-clear the model a mode already runs (the spec's baseline is the bar):
 *   npx tsx eval/bakeoff.ts --spec eval/regression/auto-reply.json --candidate <its model>
 *
 *   # an incumbent scored elsewhere (a frontier model on a subscription harness):
 *   npx tsx eval/bakeoff.ts --spec … --candidate … --incumbent-results anchor-suite.json
 *
 * The mode is the spec's `mode`. Exit 0 on pass, 1 on fail or inconclusive, 2 on a
 * usage error, 3 when no OpenRouter key is set.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { OpenRouter } from '@openrouter/sdk';
import { runSuite, type ModelSpec, type SuiteResult } from './harness.ts';
import { buildRouteEval } from './route_eval.ts';
import { loadSpec } from './spec.ts';

const argv = process.argv.slice(2);
const flag = (name: string): string | undefined => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
};
const usage = (msg: string): never => {
  console.error(`✗ ${msg}\n  usage: npx tsx eval/bakeoff.ts --spec <file> --candidate <model> ` +
    `[--incumbent <model> | --incumbent-results <suite.json>] [--k N] [--margin X] [--out <file>]`);
  process.exit(2);
};

const specPath = flag('--spec') ?? usage('--spec is required');
const candidateModel = flag('--candidate') ?? usage('--candidate is required');
const incumbentModel = flag('--incumbent');
const incumbentResults = flag('--incumbent-results');
if (incumbentModel && incumbentResults) usage('pass --incumbent or --incumbent-results, not both');
const margin = Number(flag('--margin') ?? 0);
const out = flag('--out') ?? 'route-eval.json';

// A frontier anchor runs on the subscription harness it is used from, never billed per
// token through OpenRouter. Score it there and pass --incumbent-results.
for (const m of [candidateModel, incumbentModel])
  if (m && m.toLowerCase().startsWith('anthropic/'))
    usage(`${m}: Anthropic models are not called through OpenRouter here — score the anchor ` +
      'on its own harness and pass its suite with --incumbent-results');

let loaded;
try {
  loaded = loadSpec(specPath);
} catch (e: any) {
  usage(e.message);
}
const { spec, tasks, graders, parse, sha256 } = loaded!;
const k = Number(flag('--k') ?? spec.k ?? 3);
const threshold = spec.threshold ?? 0.66;

if (!process.env.OPENROUTER_API_KEY) {
  console.error('✗ OPENROUTER_API_KEY not set — a bake-off needs live model calls.');
  process.exit(3);
}
const client = new OpenRouter({ apiKey: process.env.OPENROUTER_API_KEY });

const arm = (model: string): ModelSpec => ({ model, label: model, priceIn: 0, priceOut: 0, provider: null });
const run = (model: string): Promise<SuiteResult> =>
  runSuite({ client, spec: arm(model), tasks, parse, graders, k, threshold,
             onProgress: () => process.stdout.write('.') });

console.log(`══ bake-off · ${spec.mode} · ${tasks.length} tasks · k=${k}`);
const candidate = await run(candidateModel);
process.stdout.write('\n');
let incumbent: SuiteResult | undefined;
if (incumbentModel) {
  incumbent = await run(incumbentModel);
  process.stdout.write('\n');
} else if (incumbentResults) {
  incumbent = JSON.parse(readFileSync(incumbentResults, 'utf8')) as SuiteResult;
}

let result;
try {
  result = buildRouteEval({ mode: spec.mode, candidate, incumbent, spec, margin,
                            harness: 'model-eval-gate eval/bakeoff.ts', specPath, specSha256: sha256 });
} catch (e: any) {
  usage(e.message);
}
writeFileSync(out, JSON.stringify(result, null, 2) + '\n');
console.log(`candidate ${candidateModel}: ${result!.candidate_score}` +
  (incumbent ? `   incumbent ${incumbent.model}: ${result!.incumbent_score}` : ''));
console.log(`verdict: ${result!.verdict.toUpperCase()} — ${result!.reasons.join('; ')}`);
console.log(`wrote ${out}   (RunTune: runtune apply <id> --eval ${out})`);
process.exit(result!.verdict === 'pass' ? 0 : 1);
