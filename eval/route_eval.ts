/**
 * The route-eval result: the one document a route change carries into RunTune.
 *
 * RunTune proposes route changes and applies them only with an eval behind them; it
 * does not run model evals itself. This module turns two scored arms (candidate and
 * incumbent, same tasks, same graders, same k) into the `runtune.route-eval/1`
 * document RunTune's `apply --eval` checks (runtune/verify/route_eval.py). The
 * verdict is decided HERE, from rules stated below; RunTune checks that the decision
 * is about the change it is applying and that the numbers do not contradict it.
 *
 * Pure: no network, no clock unless `now` is omitted. The bake-off runner
 * (eval/bakeoff.ts) and regression (eval/regression.ts) both call it.
 */
import type { SuiteResult } from './harness.ts';

export const ROUTE_EVAL_SCHEMA = 'runtune.route-eval/1';
// A run where more than this share of trials never reached a provider says nothing
// about the model. Same line regression.ts draws.
export const INCONCLUSIVE_SHARE = 0.2;
// RunTune refuses fewer cases by default; below it the verdict cannot be `pass`.
export const MIN_CASES = 20;

export type RouteEval = {
  schema: typeof ROUTE_EVAL_SCHEMA;
  mode: string;
  candidate: string;
  incumbent?: string;
  verdict: 'pass' | 'fail' | 'inconclusive';
  reasons: string[];
  cases: number;
  k: number;
  metric: string;
  candidate_score: number;
  incumbent_score?: number;
  non_inferiority_margin?: number;
  candidate_pass_hat_k: number;
  incumbent_pass_hat_k?: number;
  consistency_floor?: number;
  baseline_score?: number;
  providers_seen: string[];
  cost_usd: { candidate: number; incumbent?: number; reported: boolean };
  created: string;
  harness: string;
  spec?: string;
  spec_sha256?: string;
};

export type RouteEvalInput = {
  mode: string;
  candidate: SuiteResult;
  /** The model the candidate would replace, scored on the same tasks. Omit to re-clear
   * the model a mode already runs (then the spec's baseline is the bar). */
  incumbent?: SuiteResult;
  spec: {
    graders: { kind: string; name?: string }[];
    consistency_floor?: number;
    drift_tolerance?: number;
    baseline?: { meanScore?: number | null };
  };
  /** How far below the incumbent the candidate may score and still pass. */
  margin?: number;
  harness: string;
  specPath?: string;
  specSha256?: string;
  now?: Date;
};

const round = (x: number) => Number(x.toFixed(4));

function inconclusive(s: SuiteResult): boolean {
  const trials = s.tasks.reduce((a, t) => a + t.trials.length, 0) || 1;
  return s.outcomes.provider_unavailable / trials > INCONCLUSIVE_SHARE;
}

/**
 * Decide and write down. The rules, in order:
 *   1. both arms must have scored the SAME task ids (otherwise this throws: a
 *      comparison over different cases is not an eval of this change);
 *   2. an arm that mostly never reached a provider makes the result inconclusive;
 *   3. fewer than MIN_CASES tasks cannot pass;
 *   4. the candidate's pass^k must reach the spec's consistency_floor, if it has one;
 *   5. with an incumbent: candidate meanScore >= incumbent meanScore - margin;
 *      without one (re-clearing): candidate meanScore >= baseline - drift_tolerance,
 *      when the spec has a recorded baseline.
 */
export function buildRouteEval(i: RouteEvalInput): RouteEval {
  const c = i.candidate;
  const inc = i.incumbent;
  if (inc) {
    const a = c.tasks.map((t) => t.task_id).sort().join('\n');
    const b = inc.tasks.map((t) => t.task_id).sort().join('\n');
    if (a !== b) throw new Error('candidate and incumbent were scored on different tasks');
    if (c.k !== inc.k) throw new Error(`candidate ran k=${c.k}, incumbent k=${inc.k}`);
  }
  const reasons: string[] = [];
  let verdict: RouteEval['verdict'] = 'pass';
  const fail = (why: string) => {
    reasons.push(why);
    if (verdict === 'pass') verdict = 'fail';
  };

  for (const [arm, s] of [['candidate', c], ['incumbent', inc]] as const) {
    if (s && inconclusive(s)) {
      verdict = 'inconclusive';
      reasons.push(`${arm}: ${s.outcomes.provider_unavailable} trials never reached a provider`);
    }
  }
  const cases = c.tasks.length;
  if (cases < MIN_CASES) fail(`${cases} cases; at least ${MIN_CASES} are needed`);
  const floor = i.spec.consistency_floor;
  if (floor != null && c.passHatK < floor)
    fail(`candidate pass^k ${c.passHatK.toFixed(2)} is below the consistency floor ${floor}`);
  const margin = i.margin ?? 0;
  const base = i.spec.baseline?.meanScore;
  if (inc) {
    if (c.meanScore < inc.meanScore - margin)
      fail(`candidate scored ${c.meanScore.toFixed(3)} against the incumbent's ${inc.meanScore.toFixed(3)}`
        + (margin ? ` (margin ${margin})` : ''));
  } else if (base != null) {
    const tol = i.spec.drift_tolerance ?? 0.1;
    if (c.meanScore < base - tol)
      fail(`score ${c.meanScore.toFixed(3)} is below the recorded baseline ${base} - ${tol}`);
  }
  if (verdict === 'pass') reasons.push('every rule in eval/route_eval.ts held');

  const graderNames = i.spec.graders.map((g) => g.name ?? g.kind).join(', ');
  const out: RouteEval = {
    schema: ROUTE_EVAL_SCHEMA,
    mode: i.mode,
    candidate: c.model,
    verdict,
    reasons,
    cases,
    k: c.k,
    metric: `mean grader score over k trials (${graderNames})`,
    candidate_score: round(c.meanScore),
    candidate_pass_hat_k: round(c.passHatK),
    providers_seen: c.providersSeen,
    cost_usd: {
      candidate: round(c.totalCost),
      reported: c.totalCostReported && (inc ? inc.totalCostReported : true),
    },
    created: (i.now ?? new Date()).toISOString(),
    harness: i.harness,
  };
  if (inc) {
    out.incumbent = inc.model;
    out.incumbent_score = round(inc.meanScore);
    out.incumbent_pass_hat_k = round(inc.passHatK);
    out.non_inferiority_margin = margin;
    out.cost_usd.incumbent = round(inc.totalCost);
  }
  if (floor != null) out.consistency_floor = floor;
  if (!inc && base != null) out.baseline_score = base;
  if (i.specPath) out.spec = i.specPath;
  if (i.specSha256) out.spec_sha256 = i.specSha256;
  return out;
}
