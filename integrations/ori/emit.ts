/**
 * Ori emitter — turn a frozen model-eval-gate regression spec into a runnable
 * Ori `*.eval.ts` file.
 *
 * WHY THIS DIRECTION EXISTS. This repo governs delegation; it does not want to be
 * an eval product. Ori (OpenRouter) is good at the half this repo deliberately
 * does not do: running a real agent loop, asserting over the trajectory, and
 * comparing live catalog candidates. Emitting an Ori file means a mode that
 * already earned a permission here can be re-measured there — including the
 * trajectory assertions this harness cannot produce on its own — without either
 * tool becoming a dependency of the other.
 *
 * WHAT IT EMITS. Two shapes, from the same spec:
 *
 *   'regression' — pin the mode's ALLOWLISTED model and assert it still holds.
 *                  `assertModelIsLive()` fails loudly if the catalog drops it.
 *   'bakeoff'    — the same tasks against live `candidateModels()`, for choosing
 *                  a challenger. A bake-off is a PROPOSAL generator, never an
 *                  approval: see import.ts.
 *
 * WHAT IT DOES NOT DO. It does not run anything, does not need `ori` installed,
 * and does not phone home. It writes a text file. If Ori is absent the spec is
 * still fully runnable by this repo's own harness (`eval/regression.ts`) — that
 * is the "adapt or run standalone" contract in integrations/README.md.
 *
 * Generated against the documented Ori eval API (openrouter.ai/docs/guides/ori/eval,
 * read 2026-09-01): setupAgent, setupJudge, candidateModels, assertModelIsLive,
 * run.tool().toBeCalled()/.toNotBeCalled(), run.toComplete(), run.toMention(),
 * run.toCostAtMost(), run.toFinishWithin().
 */
import type { ModeDef } from '../../src/schema.ts';

export type EmitShape = 'regression' | 'bakeoff';

export type EmitOptions = {
  shape?: EmitShape;
  /** bake-off only: how many live candidates to pull. */
  candidateLimit?: number;
  /** bake-off only: max prompt price per token (Ori's `maxPromptPrice`). */
  maxPromptPrice?: number;
};

const q = (s: string): string => JSON.stringify(String(s ?? ''));

/** Render `{{var}}` against a task's vars, exactly as eval/regression.ts does. */
function render(tpl: string, vars: Record<string, any>): string {
  return tpl.replace(/\{\{(\w+)\}\}/g, (_, k) => String(vars?.[k] ?? ''));
}

/**
 * Translate this repo's declarative graders into Ori assertions.
 *
 * Only the graders with a FAITHFUL Ori equivalent are translated. Anything else
 * (fieldAgreement, enumMatch, numericWithinTolerance, …) is deterministic
 * gold-comparison that Ori's run-level assertion API does not express, so it is
 * reported as unmapped rather than approximated — an eval that silently checks
 * something weaker than the spec is worse than one that admits the gap.
 */
function assertionsFor(graders: any[]): { lines: string[]; unmapped: string[] } {
  const lines: string[] = [];
  const unmapped: string[] = [];
  for (const g of graders) {
    switch (g.kind) {
      case 'toolCalled':
        for (const t of g.tools ?? []) lines.push(`run.tool(${q(t)}).toBeCalled();`);
        break;
      case 'toolNotCalled':
        for (const t of g.tools ?? []) lines.push(`run.tool(${q(t)}).toNotBeCalled();`);
        break;
      case 'toolSequence':
        // Ori asserts per-tool, not on relative order — translate what is
        // expressible (each step ran) and flag the ordering half as unmapped.
        for (const t of g.sequence ?? []) lines.push(`run.tool(${q(t)}).toBeCalled();`);
        unmapped.push(`toolSequence (order of ${(g.sequence ?? []).join(' → ')} is not asserted)`);
        break;
      case 'mentions':
        for (const s of g.substrings ?? []) lines.push(`run.toMention(${q(s)});`);
        break;
      case 'costAtMost':
        lines.push(`run.toCostAtMost(${Number(g.maxUsd)});`);
        break;
      case 'latencyAtMost':
        lines.push(`run.toFinishWithin(${Number(g.maxMs)});`);
        break;
      case 'llmJudge':
        // handled separately — needs the judge harness, not a run assertion
        break;
      default:
        unmapped.push(String(g.kind));
    }
  }
  // Two graders can legitimately produce the same assertion (a tool named in both
  // `toolCalled` and `toolSequence`). Emitting it twice is noise, not extra rigour.
  return { lines: [...new Set(lines)], unmapped };
}

export type EmitResult = {
  filename: string;
  source: string;
  unmapped: string[];
  /**
   * True when NOTHING beyond `run.toComplete()` survived translation — i.e. the
   * emitted file would pass as long as the agent returns anything at all. That is
   * a worse artifact than no file, because it looks like coverage. The CLI refuses
   * to write it without --allow-empty.
   */
  vacuous: boolean;
};

export function emitOriEval(
  spec: any,
  mode: ModeDef | undefined,
  opts: EmitOptions = {},
): EmitResult {
  const shape: EmitShape = opts.shape ?? 'regression';
  const { lines, unmapped } = assertionsFor(spec.graders ?? []);
  const judge = (spec.graders ?? []).find((g: any) => g.kind === 'llmJudge');

  const imports = [
    `import { test } from 'bun:test';`,
    `import { ${[
      'setupAgent',
      judge ? 'setupJudge' : null,
      shape === 'bakeoff' ? 'candidateModels' : 'assertModelIsLive',
    ]
      .filter(Boolean)
      .join(', ')} } from 'ori/eval';`,
  ].join('\n');

  // Every task's fully-rendered prompt, so the Ori file is self-contained and
  // does not need this repo present to run.
  const cases = (spec.tasks ?? []).map((t: any) => ({
    id: t.id,
    prompt: render(spec.input_template, t.vars),
  }));

  const header = `/**
 * GENERATED by model-eval-gate — integrations/ori/emit.ts. Do not hand-edit:
 * regenerate from the spec so the governed policy and the Ori eval cannot drift.
 *
 *   mode:  ${spec.mode}
 *   spec:  ${spec.title}
 *   shape: ${shape}
 *
 * Source of truth for whether this delegation is ALLOWED remains routes.json in
 * model-eval-gate. This file measures; it does not grant permission. A passing
 * run here is evidence for a mode, not a mode.
 *${
   unmapped.length
     ? `
 * NOT ASSERTED HERE (no faithful Ori equivalent — still enforced by the spec via
 * \`npx tsx eval/regression.ts --mode ${spec.mode}\`):
${unmapped.map((u) => ` *   - ${u}`).join('\n')}
 *`
     : ''
 }
 */`;

  const caseBlock = (indent: string, runExpr: string) =>
    [
      `const run = await ${runExpr};`,
      ...lines,
      `run.toComplete();`,
      judge
        ? `await judge.autoEvals({ criteria: ${q(judge.rubric ?? 'Answers accurately and invents nothing.')}, run });`
        : null,
    ]
      .filter((l): l is string => Boolean(l))
      .map((l) => indent + l)
      .join('\n');

  let body: string;
  if (shape === 'bakeoff') {
    body = `const candidates = await candidateModels({
  limit: ${opts.candidateLimit ?? 5},${
    opts.maxPromptPrice != null ? `\n  maxPromptPrice: ${opts.maxPromptPrice},` : ''
  }
});

const CASES = ${JSON.stringify(cases, null, 2)};

for (const model of candidates) {
  for (const c of CASES) {
    test(\`${spec.mode} · \${c.id} · \${model}\`, async () => {
${caseBlock('      ', 'setupAgent({ model }).run(c.prompt)')}
    });
  }
}`;
  } else {
    const model = mode?.model;
    body = `${
      model
        ? `// The model this mode is CURRENTLY allowlisted to. If routes.json is repointed,
// regenerate this file — a stale pin here would measure a model nobody approved.
assertModelIsLive(${q(model)});

const agent = setupAgent({ model: ${q(model)} });`
        : `// No allowlisted model resolved at emit time — using the workspace default.
const agent = setupAgent();`
    }${judge ? `\nconst judge = setupJudge({ minScore: ${spec.threshold ?? 0.66} });` : ''}

const CASES = ${JSON.stringify(cases, null, 2)};

for (const c of CASES) {
  test(\`${spec.mode} · \${c.id}\`, async () => {
${caseBlock('    ', 'agent.run(c.prompt)')}
  });
}`;
  }

  // In the bake-off shape the judge is shared across candidates, so it is set up
  // once above the loop rather than inside the per-model `setupAgent` block.
  const judgeSetup =
    shape === 'bakeoff' && judge
      ? `const judge = setupJudge({ minScore: ${spec.threshold ?? 0.66} });`
      : null;

  const vacuous = lines.length === 0 && !judge;
  const vacuousBanner = vacuous
    ? `
// ⚠ THIS FILE ASSERTS ALMOST NOTHING.
// Every grader in the source spec is a deterministic gold-comparison that Ori's
// run-level assertion API cannot express, so all that survived translation is
// \`run.toComplete()\` — it passes if the agent returns anything at all.
// Do not read a green run here as evidence about ${spec.mode}.
// The real check for this mode is:  npx tsx eval/regression.ts --mode ${spec.mode}
`
    : '';

  const source = [header, imports, vacuousBanner.trim() || null, judgeSetup, body, '']
    .filter((x) => x !== null && x !== '')
    .join('\n\n');

  return {
    filename: `evals/${spec.mode}/${spec.mode}.${shape}.eval.ts`,
    source,
    unmapped,
    vacuous,
  };
}
