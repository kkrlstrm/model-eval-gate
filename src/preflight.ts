/**
 * Preflight — turn a mode's advisory `use_when` / `do_not_use_when` into
 * machine-checkable constraints. `use_when` prose can't stop an agent from
 * picking `extract-bulk` for a 3-row task; a `constraints` block can. This is a
 * best-effort eligibility check on task metadata the caller supplies — it doesn't
 * need to understand the task perfectly, just catch the obvious violations.
 *
 * Kept as pure functions so the CLI, the tests, and the Python port (which mirrors
 * this logic) all agree.
 */
import type { ModeDef } from './schema.ts';

export type Stakes = 'low' | 'medium' | 'high';
// snake_case to match the JSON policy contract (routes.json uses min_rows /
// allowed_input_types) and the Python port, so the two stay byte-for-byte in sync.
export type TaskMeta = {
  rows?: number;
  stakes?: Stakes;
  input_type?: 'text' | 'image';
  single_row_decision?: boolean;
  human_review?: boolean;
};

const STAKES_RANK: Record<Stakes, number> = { low: 0, medium: 1, high: 2 };

export type PreflightResult = {
  ok: boolean;
  violations: string[]; // constraints the metadata definitively breaks
  unchecked: string[]; // constraints declared but not covered by the metadata provided
};

export function canDelegate(mode: ModeDef, meta: TaskMeta = {}): PreflightResult {
  const c = mode.constraints;
  const violations: string[] = [];
  const unchecked: string[] = [];
  if (!c) return { ok: true, violations, unchecked };

  if (c.min_rows != null) {
    if (meta.rows == null) unchecked.push(`min_rows=${c.min_rows} (no row count supplied)`);
    else if (meta.rows < c.min_rows)
      violations.push(`min_rows: task has ${meta.rows} rows, needs ≥ ${c.min_rows}`);
  }
  if (c.forbid_single_row_decision) {
    if (meta.single_row_decision == null) unchecked.push('forbid_single_row_decision (not stated)');
    else if (meta.single_row_decision)
      violations.push('forbid_single_row_decision: this output drives a single-row decision');
  }
  if (c.requires_human_review) {
    if (meta.human_review == null) unchecked.push('requires_human_review (not stated)');
    else if (!meta.human_review)
      violations.push('requires_human_review: no human review in the loop');
  }
  if (c.allowed_input_types) {
    if (meta.input_type == null)
      unchecked.push(
        `allowed_input_types=${c.allowed_input_types.join('|')} (no input type supplied)`,
      );
    else if (!c.allowed_input_types.includes(meta.input_type))
      violations.push(
        `allowed_input_types: "${meta.input_type}" not in ${c.allowed_input_types.join('|')}`,
      );
  }
  if (c.max_stakes) {
    if (meta.stakes == null) unchecked.push(`max_stakes=${c.max_stakes} (no stakes supplied)`);
    else if (STAKES_RANK[meta.stakes] > STAKES_RANK[c.max_stakes])
      violations.push(`max_stakes: task is "${meta.stakes}", mode allows ≤ "${c.max_stakes}"`);
  }
  return { ok: violations.length === 0, violations, unchecked };
}
