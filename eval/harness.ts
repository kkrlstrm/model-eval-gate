/**
 * Eval harness — runs a task set through a model over k TRIALS and reports
 * non-determinism metrics (pass@k / pass^k) plus cost, latency, and the served
 * provider. Single-trial evals hide run-to-run variance; per the Anthropic evals
 * guidance, use pass@k where one success matters and pass^k where consistency is
 * essential (customer-facing / binary-gating modes).
 *
 *   pass@k(task)  = did AT LEAST ONE of k trials pass?      (shots on goal)
 *   pass^k(task)  = did ALL k trials pass?                  (consistency)
 *   score         = combined grader score, mean across graders, mean across trials
 *
 * The served-provider capture is the eval->prod drift guard: it records which
 * OpenRouter endpoint actually answered, so a routes.json provider pin can be set
 * to the endpoint the eval was scored on (see the infrastructure-noise finding).
 */
import type { OpenRouter } from '@openrouter/sdk';
import { callModel } from '@openrouter/agent';
import type { Grader, GraderResult } from './graders.ts';

export type Task = { id: string; input: string | any[]; gold: any };
export type ModelSpec = {
  model: string;
  label?: string;
  priceIn: number;
  priceOut: number;
  provider?: any | null;
};

// A trial can fail for three unrelated reasons; keep them separate so a transient
// OpenRouter outage never masquerades as a model regression.
export type TrialOutcome = 'ok' | 'provider_unavailable' | 'parse_failure' | 'grader_failure';
export type TrialResult = {
  pass: boolean;
  score: number;
  ms: number;
  cost: number;
  costReported: boolean; // true = OpenRouter-reported cost; false = route-file estimate
  provider: string | null;
  graders: GraderResult[];
  parseOk: boolean;
  outcome: TrialOutcome;
};
export type TaskResult = {
  task_id: string;
  trials: TrialResult[];
  passes: number;
  passAtK: number; // 0 | 1
  passHatK: number; // 0 | 1
  meanScore: number;
  scoreStdev: number;
};
export type SuiteResult = {
  model: string;
  label: string;
  k: number;
  threshold: number;
  tasks: TaskResult[];
  passAtK: number; // mean over tasks
  passHatK: number; // mean over tasks
  meanScore: number;
  totalCost: number;
  totalCostReported: boolean; // false if any trial fell back to a route-file estimate
  totalMs: number;
  providersSeen: string[];
  outcomes: Record<TrialOutcome, number>; // trial counts by outcome
};

const stdev = (xs: number[]): number => {
  if (xs.length < 2) return 0;
  const m = xs.reduce((a, b) => a + b, 0) / xs.length;
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / xs.length);
};

async function runTrial(
  client: OpenRouter,
  spec: ModelSpec,
  task: Task,
  parse: (text: string) => any,
  graders: Grader[],
  threshold: number,
): Promise<TrialResult> {
  const callArgs: any = { model: spec.model, input: task.input };
  if (spec.provider) callArgs.provider = spec.provider;
  const t0 = Date.now();
  let text = '';
  let inTok = 0,
    outTok = 0,
    provider: string | null = null;
  let reportedCost: number | null = null;
  let callFailed = false;
  try {
    const r = callModel(client, callArgs);
    text = await r.getText();
    try {
      const resp = (await r.getResponse()) as any;
      inTok = resp?.usage?.inputTokens ?? resp?.usage?.prompt_tokens ?? 0;
      outTok = resp?.usage?.outputTokens ?? resp?.usage?.completion_tokens ?? 0;
      provider = resp?.provider ?? resp?.raw?.provider ?? null;
      if (typeof resp?.usage?.cost === 'number') reportedCost = resp.usage.cost;
    } catch {}
  } catch (e: any) {
    callFailed = true; // the API/provider failed — NOT a model-quality signal
  }
  const ms = Date.now() - t0;
  const estCost = (inTok * spec.priceIn + outTok * spec.priceOut) / 1_000_000;
  const cost = reportedCost ?? estCost; // prefer OpenRouter-reported cost
  const parsed = (() => {
    try {
      return parse(text);
    } catch {
      return null;
    }
  })();

  const grs: GraderResult[] = [];
  let graderThrew = false;
  for (const g of graders) {
    try {
      grs.push(await g.grade(parsed, { gold: task.gold, client }));
    } catch {
      graderThrew = true;
    }
  }
  const score = grs.length ? grs.reduce((a, b) => a + b.score, 0) / grs.length : 0;

  const outcome: TrialOutcome = callFailed
    ? 'provider_unavailable'
    : graderThrew
      ? 'grader_failure'
      : parsed == null
        ? 'parse_failure'
        : 'ok';
  return {
    pass: outcome === 'ok' && score >= threshold,
    score,
    ms,
    cost,
    costReported: reportedCost != null,
    provider,
    graders: grs,
    parseOk: parsed != null,
    outcome,
  };
}

export async function runSuite(opts: {
  client: OpenRouter;
  spec: ModelSpec;
  tasks: Task[];
  parse: (text: string) => any;
  graders: Grader[];
  k: number;
  threshold: number;
  onProgress?: (msg: string) => void;
}): Promise<SuiteResult> {
  const { client, spec, tasks, parse, graders, k, threshold } = opts;
  const label = spec.label ?? spec.model;
  const taskResults: TaskResult[] = [];
  const providersSeen = new Set<string>();
  const outcomes: Record<TrialOutcome, number> = {
    ok: 0,
    provider_unavailable: 0,
    parse_failure: 0,
    grader_failure: 0,
  };
  let totalCost = 0,
    totalMs = 0,
    anyEstimated = false;

  for (const task of tasks) {
    const trials: TrialResult[] = [];
    for (let i = 0; i < k; i++) {
      const tr = await runTrial(client, spec, task, parse, graders, threshold);
      trials.push(tr);
      totalCost += tr.cost;
      totalMs += tr.ms;
      outcomes[tr.outcome]++;
      if (!tr.costReported) anyEstimated = true;
      if (tr.provider) providersSeen.add(tr.provider);
      opts.onProgress?.(
        `[${label}] ${task.id} trial ${i + 1}/${k} score=${tr.score.toFixed(2)} ${tr.pass ? 'pass' : 'FAIL'}`,
      );
    }
    const passes = trials.filter((t) => t.pass).length;
    const scores = trials.map((t) => t.score);
    taskResults.push({
      task_id: task.id,
      trials,
      passes,
      passAtK: passes >= 1 ? 1 : 0,
      passHatK: passes === k ? 1 : 0,
      meanScore: scores.reduce((a, b) => a + b, 0) / scores.length,
      scoreStdev: stdev(scores),
    });
  }

  const n = taskResults.length || 1;
  return {
    model: spec.model,
    label,
    k,
    threshold,
    tasks: taskResults,
    passAtK: taskResults.reduce((a, t) => a + t.passAtK, 0) / n,
    passHatK: taskResults.reduce((a, t) => a + t.passHatK, 0) / n,
    meanScore: taskResults.reduce((a, t) => a + t.meanScore, 0) / n,
    totalCost,
    totalCostReported: !anyEstimated,
    totalMs,
    providersSeen: [...providersSeen],
    outcomes,
  };
}
