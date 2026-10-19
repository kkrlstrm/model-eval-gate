/**
 * A regression spec, made runnable: its tasks rendered, its graders built, its output
 * parser chosen. Shared by regression.ts (does the routed model still pass?) and
 * bakeoff.ts (does a candidate pass against the incumbent?), so both score a spec the
 * same way.
 */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { Task } from './harness.ts';
import { buildGrader, type Grader } from './graders.ts';
import { validateSpec } from '../src/schema.ts';

export function parseJsonObject(text: string): any {
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

export function render(tpl: string, vars: Record<string, any>): string {
  return tpl.replace(/\{\{(\w+)\}\}/g, (_, k) => String(vars?.[k] ?? ''));
}

export type LoadedSpec = {
  spec: any;
  tasks: Task[];
  graders: Grader[];
  parse: (text: string) => any;
  sha256: string;
};

/** Read and validate a spec file. Throws with the validation errors on a bad spec. */
export function loadSpec(path: string): LoadedSpec {
  const raw = readFileSync(path, 'utf8');
  const spec = JSON.parse(raw);
  const v = validateSpec(spec);
  if (!v.ok) throw new Error(`${path}: invalid regression spec — ${v.errors.join('; ')}`);
  return {
    spec,
    tasks: spec.tasks.map((t: any) => ({
      id: t.id,
      input: render(spec.input_template, t.vars),
      gold: t.gold,
      trajectory: t.trajectory,
    })),
    graders: spec.graders.map(buildGrader),
    // A prose/agent spec must not be read with the JSON parser, or every trial
    // registers as parse_failure and gets reported as drift.
    parse: spec.output_format === 'text' ? (s: string) => s ?? null : parseJsonObject,
    sha256: createHash('sha256').update(raw).digest('hex'),
  };
}
