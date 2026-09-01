/**
 * Ori importer — turn an Ori eval run into a model-eval-gate PROPOSAL.
 *
 * THE GOVERNANCE POINT, first, because it is the whole reason this file is shaped
 * the way it is. Ori's own scheduled workflow is: re-run monthly, and when a model
 * scores better than the incumbent, open a PR you merge. That is a good default for
 * a coding agent's model. It is precisely the failure mode this repo exists to stop
 * for a governed mode — a model swap merged on a single run, with no
 * `do_not_use_when`, no pass^k consistency check, no provider pin, and no dated
 * record of what was replaced or why. "Scored better" is not a permission.
 *
 * So this importer NEVER writes routes.json and never grants a mode. It emits a
 * proposal a human reads and applies via docs/ADDING_A_MODE.md:
 *
 *   1. a frozen regression spec  (the maintained half — what keeps the permission honest)
 *   2. a routes.json mode STUB   (with use_when / do_not_use_when left blank, on purpose)
 *   3. the numbers, restated as evidence, including what the run does NOT establish
 *
 * The blanks are load-bearing. `do_not_use_when` is the negative constraint that
 * stops a model cleared for one task shape being promoted to a neighbouring one,
 * and no eval tool can infer it — it comes from reading the failures. A stub that
 * auto-filled it would manufacture the exact false confidence the gate is for.
 *
 * INPUT. Ori records runs to `.ori/eval/history.jsonl` (documented at
 * openrouter.ai/docs/guides/ori/eval, read 2026-09-01). The per-record SHAPE is not
 * documented, so this reads it DEFENSIVELY: every field is probed across a few
 * plausible names, anything missing stays absent rather than being defaulted to a
 * flattering value, and a record that yields no model id is skipped and counted.
 * Unparseable history is reported, never silently treated as "no drift".
 */
import { readFileSync, existsSync } from 'node:fs';

export type OriTrial = {
  model: string;
  test?: string;
  passed?: boolean;
  score?: number;
  costUsd?: number;
  ms?: number;
  tools?: string[];
};

export type OriRunSummary = {
  models: {
    model: string;
    trials: number;
    passed: number;
    passRate: number;
    meanScore?: number;
    totalCostUsd?: number;
    meanMs?: number;
  }[];
  /** Every normalized trial, so a spec can be built without re-reading the file. */
  trials: OriTrial[];
  skipped: number;
  parseErrors: number;
  totalRecords: number;
};

const num = (v: any): number | undefined => {
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
};

/** Probe a record for the first key that carries a usable value. */
const pick = (rec: any, keys: string[]): any => {
  for (const k of keys) {
    const v = k.split('.').reduce((o: any, part) => (o == null ? undefined : o[part]), rec);
    if (v !== undefined && v !== null) return v;
  }
  return undefined;
};

/**
 * Normalize one history record. Returns null when no model id can be recovered —
 * a record we cannot attribute to a model is not evidence about any model.
 */
export function normalizeRecord(rec: any): OriTrial | null {
  const model = pick(rec, ['model', 'modelId', 'model_slug', 'meta.model', 'run.model']);
  if (typeof model !== 'string' || !model.trim()) return null;

  const passedRaw = pick(rec, ['passed', 'pass', 'ok', 'success', 'result.passed']);
  const statusRaw = pick(rec, ['status', 'result', 'outcome']);
  const passed =
    typeof passedRaw === 'boolean'
      ? passedRaw
      : typeof statusRaw === 'string'
        ? /^(pass|passed|ok|success)$/i.test(statusRaw)
        : undefined;

  const toolsRaw = pick(rec, ['tools', 'toolCalls', 'tool_calls', 'trajectory']);
  const tools = Array.isArray(toolsRaw)
    ? toolsRaw
        .map((t: any) => (typeof t === 'string' ? t : t?.name))
        .filter((t: any): t is string => typeof t === 'string')
    : undefined;

  return {
    model: model.trim(),
    test: pick(rec, ['test', 'name', 'testName', 'title']),
    passed,
    score: num(pick(rec, ['score', 'judgeScore', 'judge.score'])),
    costUsd: num(pick(rec, ['cost', 'costUsd', 'usage.cost', 'totalCost'])),
    ms: num(pick(rec, ['ms', 'durationMs', 'duration', 'elapsedMs'])),
    tools,
  };
}

/** Read + aggregate an Ori history.jsonl into a per-model summary. */
export function summarizeHistory(path: string): OriRunSummary {
  if (!existsSync(path)) {
    throw new Error(
      `no Ori history at ${path} — run \`ori eval\` first, or pass --history <path>. ` +
        `Ori writes runs to .ori/eval/history.jsonl.`,
    );
  }
  const lines = readFileSync(path, 'utf8')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);

  const byModel = new Map<string, OriTrial[]>();
  let skipped = 0;
  let parseErrors = 0;

  for (const line of lines) {
    let rec: any;
    try {
      rec = JSON.parse(line);
    } catch {
      parseErrors++;
      continue;
    }
    // A record may itself hold an array of per-test results.
    const inner = Array.isArray(rec?.results) ? rec.results : [rec];
    for (const r of inner) {
      const t = normalizeRecord({ ...rec, ...r });
      if (!t) {
        skipped++;
        continue;
      }
      if (!byModel.has(t.model)) byModel.set(t.model, []);
      byModel.get(t.model)!.push(t);
    }
  }

  const mean = (xs: number[]): number | undefined =>
    xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : undefined;

  const models = [...byModel.entries()]
    .map(([model, trials]) => {
      const scored = trials.map((t) => t.score).filter((s): s is number => s != null);
      const costs = trials.map((t) => t.costUsd).filter((c): c is number => c != null);
      const times = trials.map((t) => t.ms).filter((m): m is number => m != null);
      const decided = trials.filter((t) => t.passed != null);
      const passed = decided.filter((t) => t.passed).length;
      return {
        model,
        trials: trials.length,
        passed,
        // Rate over DECIDED trials only — an undecided trial is not a pass.
        passRate: decided.length ? passed / decided.length : 0,
        meanScore: mean(scored),
        totalCostUsd: costs.length ? costs.reduce((a, b) => a + b, 0) : undefined,
        meanMs: mean(times),
      };
    })
    .sort((a, b) => b.passRate - a.passRate || (b.meanScore ?? 0) - (a.meanScore ?? 0));

  return {
    models,
    trials: [...byModel.values()].flat(),
    skipped,
    parseErrors,
    totalRecords: lines.length,
  };
}

/**
 * Build the routes.json mode STUB. Deliberately incomplete: the fields that
 * require human judgement are emitted empty with a TODO, so the stub cannot be
 * pasted in and forgotten.
 */
export function proposeModeStub(
  summary: OriRunSummary,
  today: string,
): { stub: Record<string, any>; winner: string | null; blockers: string[] } {
  const winner = summary.models[0] ?? null;
  const blockers: string[] = [];

  if (!winner) blockers.push('no model could be attributed from the history — nothing to propose');
  if (winner && winner.trials < 2)
    blockers.push(
      `winner has ${winner.trials} trial(s) — a single run measures neither consistency nor variance; re-run with more trials before this is evidence`,
    );
  if (summary.parseErrors)
    blockers.push(`${summary.parseErrors} history line(s) did not parse — the summary is partial`);
  if (summary.skipped)
    blockers.push(`${summary.skipped} record(s) had no attributable model and were skipped`);

  const stub: Record<string, any> = {
    model: winner?.model ?? 'TODO',
    priceIn: 0,
    priceOut: 0,
    purpose: `TODO — describe the TASK SHAPE this mode covers, not the model.`,
    use_when: `TODO — the conditions under which this was actually proven.`,
    do_not_use_when: `TODO — REQUIRED. Read the failing trials and write what this must NOT be used for. An eval cannot infer this; leaving it generic is how a mode gets misused.`,
    evidence_ref: `Ori eval run imported ${today} (see the regression spec's source block)`,
    verified_date: today,
    provider: null,
  };
  return { stub, winner: winner?.model ?? null, blockers };
}

/** Human-readable proposal, printed for review. Applying it is a separate, manual act. */
export function renderProposal(
  modeName: string,
  summary: OriRunSummary,
  stub: Record<string, any>,
  blockers: string[],
  specPath: string,
): string {
  const rows = summary.models
    .map(
      (m) =>
        `  ${m.model.padEnd(38)} pass=${(m.passRate * 100).toFixed(0).padStart(3)}%  n=${String(m.trials).padStart(3)}` +
        (m.meanScore != null ? `  score=${m.meanScore.toFixed(2)}` : '') +
        (m.totalCostUsd != null ? `  $${m.totalCostUsd.toFixed(4)}` : '') +
        (m.meanMs != null ? `  ${(m.meanMs / 1000).toFixed(1)}s` : ''),
    )
    .join('\n');

  return [
    `Ori run → PROPOSAL for mode "${modeName}"  (nothing has been written to routes.json)`,
    ``,
    `Measured (${summary.totalRecords} history records):`,
    rows || '  (none)',
    ``,
    blockers.length
      ? `Blockers — resolve before this becomes a mode:\n${blockers.map((b) => `  ✗ ${b}`).join('\n')}`
      : `No structural blockers found. That is not approval — see below.`,
    ``,
    `What this run does NOT establish, and the gate still requires:`,
    `  - do_not_use_when — the negative constraint. Written by a human who read the failures.`,
    `  - pass^k consistency — Ori reports whether tests passed, not whether the mode passes`,
    `    ALL k trials. Run \`npx tsx eval/regression.ts --mode ${modeName}\` for that.`,
    `  - a provider pin — a score on one endpoint is not a score on every endpoint`,
    `    OpenRouter may route to. Record the served provider, then pin it.`,
    `  - the subscription counterfactual — if this work runs inside a subscription-billed`,
    `    agent session, the frontier model's marginal cost is ~$0 and the dollar saving`,
    `    above is the cost of LEAVING, not a saving. Check \`meg workload list\`.`,
    ``,
    `Next:`,
    `  1. review the frozen regression spec written to ${specPath}`,
    `  2. fill in purpose / use_when / do_not_use_when in the stub below`,
    `  3. follow docs/ADDING_A_MODE.md to add it to routes.json`,
    ``,
    `Mode stub:`,
    JSON.stringify({ [modeName]: stub }, null, 2),
  ].join('\n');
}

/**
 * Build a frozen regression spec from an Ori run. Tasks carry the recorded
 * trajectory when Ori reported one, which is what makes the imported spec
 * runnable by this repo's harness with real trajectory graders.
 */
export function proposeSpec(modeName: string, summary: OriRunSummary, today: string): any {
  const trials = summary.trials;
  const winner = summary.models[0];
  const byTest = new Map<string, OriTrial>();
  for (const t of trials) {
    if (winner && t.model !== winner.model) continue;
    const key = t.test ?? `case-${byTest.size + 1}`;
    if (!byTest.has(key)) byTest.set(key, t);
  }

  return {
    mode: modeName,
    title: `Imported from an Ori eval run (${today})`,
    note:
      'GENERATED STUB from integrations/ori/import.ts. The prompts and gold below are ' +
      'placeholders — Ori history records the RESULT of a run, not the prompt corpus. ' +
      'Paste the real prompts from the .eval.ts file before trusting this spec, then ' +
      'record a baseline with `--update-baseline`.',
    source: { tool: 'ori', imported_at: today },
    k: 3,
    threshold: 0.66,
    drift_tolerance: 0.1,
    consistency_floor: 0.8,
    output_format: 'text',
    graders: [
      {
        kind: 'toolCalled',
        name: 'required-tools',
        tools: [...new Set(trials.flatMap((t) => t.tools ?? []))].slice(0, 5),
      },
    ],
    tasks: [...byTest.entries()].map(([test, t], i) => ({
      id: `ori-${String(i + 1).padStart(2, '0')}`,
      vars: { prompt: `TODO — paste the prompt for "${test}" from the .eval.ts file` },
      gold: {},
      ...(t.tools?.length ? { trajectory: t.tools.map((name) => ({ name })) } : {}),
    })),
    input_template: '{{prompt}}',
  };
}
