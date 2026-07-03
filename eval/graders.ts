/**
 * Grader taxonomy — the three grader kinds from Anthropic's "Demystifying evals
 * for AI agents": CODE (deterministic), MODEL (LLM-as-judge), HUMAN (recorded
 * verdict). An eval task declares which graders apply; the harness runs them and
 * combines their scores. Best practice (per that doc): prefer deterministic code
 * graders, use a model grader where nuance is needed, use a human grader
 * judiciously. Regression runs default to code + recorded-human (free, exactly
 * reproducible); the model grader is opt-in because it spends tokens per trial.
 */
import type { OpenRouter } from '@openrouter/sdk';
import { callModel } from '@openrouter/agent';

export type Assertion = { name: string; pass: boolean; detail?: string };
export type GraderKind = 'code' | 'model' | 'human';
export type GraderResult = { grader: string; kind: GraderKind; score: number; assertions: Assertion[] };

/** Context handed to every grader for a single output. */
export type GradeCtx = {
  gold: any;               // frozen reference / labels for this task
  client?: OpenRouter;     // present only when a model grader is used
};

export type Grader = {
  name: string;
  kind: GraderKind;
  grade: (parsed: any, ctx: GradeCtx) => Promise<GraderResult> | GraderResult;
};

const scoreOf = (as: Assertion[]): number =>
  as.length ? as.filter((a) => a.pass).length / as.length : 0;

// ─────────────────────────────────────────────────────────────────────────────
// CODE grader — deterministic assertions over the parsed output.
// ─────────────────────────────────────────────────────────────────────────────
export function codeGrader(name: string, fn: (parsed: any, gold: any) => Assertion[]): Grader {
  return {
    name,
    kind: 'code',
    grade: (parsed, ctx) => {
      const assertions = parsed == null
        ? [{ name: 'parseable', pass: false, detail: 'output did not parse' }]
        : fn(parsed, ctx.gold);
      return { grader: name, kind: 'code', score: scoreOf(assertions), assertions };
    },
  };
}

/**
 * Field-agreement code grader: one assertion per field, candidate[field] must
 * equal the frozen reference gold[field] (case-insensitive string compare). This
 * is the deterministic re-implementation of the original evals' "agreement with
 * Sonnet" metric, using the frozen gold as the reference so a regression run is
 * free and exactly reproducible.
 */
export function fieldAgreementGrader(name: string, fields: string[]): Grader {
  return codeGrader(name, (parsed, gold) =>
    fields.map((f) => {
      const a = String(parsed?.[f] ?? '').trim().toLowerCase();
      const b = String(gold?.[f] ?? '').trim().toLowerCase();
      return { name: f, pass: a === b, detail: a === b ? undefined : `got "${parsed?.[f]}" want "${gold?.[f]}"` };
    }),
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// MODEL grader — an LLM judges the output against a rubric (0..1). Opt-in.
// ─────────────────────────────────────────────────────────────────────────────
export function llmJudgeGrader(name: string, judgeModel: string, rubric: string): Grader {
  return {
    name,
    kind: 'model',
    grade: async (parsed, ctx) => {
      if (!ctx.client) {
        return { grader: name, kind: 'model', score: 0, assertions: [{ name: 'judge', pass: false, detail: 'no client supplied' }] };
      }
      const q = `${rubric}\n\nReference (gold):\n${JSON.stringify(ctx.gold)}\n\nCandidate output:\n${JSON.stringify(parsed)}\n\nRespond with ONLY a JSON object {"score": <0..1>, "reason": "..."}.`;
      try {
        const r = callModel(ctx.client, { model: judgeModel, input: q } as any);
        const text = await r.getText();
        const mm = text.match(/\{[\s\S]*\}/);
        const j = mm ? JSON.parse(mm[0]) : { score: 0, reason: 'unparseable judge output' };
        const score = Math.max(0, Math.min(1, Number(j.score) || 0));
        return { grader: name, kind: 'model', score, assertions: [{ name: 'judge', pass: score >= 0.5, detail: String(j.reason ?? '') }] };
      } catch (e: any) {
        return { grader: name, kind: 'model', score: 0, assertions: [{ name: 'judge', pass: false, detail: String(e?.message ?? e) }] };
      }
    },
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// HUMAN grader — a recorded human verdict, frozen in the task spec. This encodes
// the "an operator read the output and confirmed the finding was real" step (e.g.
// an operator confirming that a flagged security finding was genuine) as a
// reproducible check.
// ─────────────────────────────────────────────────────────────────────────────
export function recordedHumanGrader(name = 'human-recorded'): Grader {
  return {
    name,
    kind: 'human',
    grade: (_parsed, ctx) => {
      const v = ctx.gold?.human_verdict;
      if (v == null) {
        return { grader: name, kind: 'human', score: 1, assertions: [{ name: 'verdict', pass: true, detail: 'no recorded verdict — skipped' }] };
      }
      const pass = v === true || v === 'pass';
      return { grader: name, kind: 'human', score: pass ? 1 : 0, assertions: [{ name: 'verdict', pass, detail: `recorded=${v}` }] };
    },
  };
}
