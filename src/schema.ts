/**
 * Policy schema + validation. `routes.json` is the source of truth, so it gets
 * validated like one: structural checks (zod) plus cross-checks the governance
 * story depends on (no mode in both modes+retired, no vibes-y generic mode names,
 * ISO dates, numeric prices, a well-formed provider pin). Both the CLI and the
 * regression runner load routes THROUGH this file, and `check` runs it offline.
 */
import { readFileSync } from 'node:fs';
import { z } from 'zod';

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

// Mode names that describe a MODEL or a vibe, not a task shape. A mode named this
// way becomes a lookup table and invites misuse — the governance doc forbids it.
export const RESERVED_GENERIC = new Set([
  'chat',
  'cheap',
  'cheapest',
  'free',
  'online',
  'general',
  'default',
  'auto',
  'fast',
  'model',
  'llm',
  'any',
  'fallback',
]);

const ProviderPin = z.strictObject({
  order: z.array(z.string()).nonempty().optional(),
  only: z.array(z.string()).nonempty().optional(),
  allowFallbacks: z.boolean().optional(),
  quantizations: z.array(z.string()).nonempty().optional(),
});

const Constraints = z.strictObject({
  min_rows: z.number().int().positive().optional(),
  requires_human_review: z.boolean().optional(),
  forbid_single_row_decision: z.boolean().optional(),
  allowed_input_types: z
    .array(z.enum(['text', 'image']))
    .nonempty()
    .optional(),
  max_stakes: z.enum(['low', 'medium', 'high']).optional(),
});

const Mode = z.strictObject({
  model: z.string().min(1),
  priceIn: z.number().nonnegative(),
  priceOut: z.number().nonnegative(),
  purpose: z.string().min(1),
  use_when: z.string().min(1),
  do_not_use_when: z.string().min(1),
  evidence_ref: z.string().optional(),
  verified_date: z.string().regex(ISO_DATE, 'must be an ISO date (YYYY-MM-DD)'),
  provider: ProviderPin.nullable().optional(),
  constraints: Constraints.optional(),
});

const Retired = z.strictObject({
  retired_date: z.string().regex(ISO_DATE, 'must be an ISO date (YYYY-MM-DD)').optional(),
  reason: z.string().min(1),
});

export const RoutesSchema = z.strictObject({
  _schema: z.string().optional(),
  _readme: z.string().optional(),
  provider_pin_note: z.string().optional(),
  staleness_warn_days: z.number().int().nonnegative().optional(),
  modes: z.record(z.string(), Mode),
  retired: z.record(z.string(), Retired).optional().default({}),
});

export type Routes = z.infer<typeof RoutesSchema>;
export type ModeDef = z.infer<typeof Mode>;

// ── regression spec schema ──
const GraderCfg = z.looseObject({ kind: z.string().min(1), name: z.string().optional() });
export const SpecSchema = z.strictObject({
  mode: z.string().min(1),
  title: z.string().min(1),
  note: z.string().optional(),
  k: z.number().int().positive().optional(),
  threshold: z.number().min(0).max(1).optional(),
  drift_tolerance: z.number().min(0).max(1).optional(),
  consistency_floor: z.number().min(0).max(1).optional(),
  baseline: z.looseObject({}).optional(),
  input_template: z.string().min(1),
  graders: z.array(GraderCfg).nonempty(),
  tasks: z
    .array(z.looseObject({ id: z.string(), vars: z.record(z.string(), z.any()), gold: z.any() }))
    .nonempty(),
});

export type ValidationResult<T> = { ok: true; data: T } | { ok: false; errors: string[] };

function zodErrors(err: z.ZodError): string[] {
  return err.issues.map((i) => `${i.path.length ? i.path.join('.') : '(root)'}: ${i.message}`);
}

/** Structural + cross-field validation of a parsed routes object. */
export function validateRoutes(raw: unknown): ValidationResult<Routes> {
  const parsed = RoutesSchema.safeParse(raw);
  if (!parsed.success) return { ok: false, errors: zodErrors(parsed.error) };
  const data = parsed.data;
  const errors: string[] = [];

  const modeNames = Object.keys(data.modes);
  if (modeNames.length === 0) errors.push('modes: at least one mode is required');

  for (const name of modeNames) {
    if (RESERVED_GENERIC.has(name.toLowerCase())) {
      errors.push(
        `modes.${name}: reserved generic name — name a mode for its task shape, not a model/vibe (see GOVERNANCE.md)`,
      );
    }
  }
  for (const name of Object.keys(data.retired)) {
    if (name in data.modes) {
      errors.push(`"${name}" appears in BOTH modes and retired — a mode is one or the other`);
    }
  }
  return errors.length ? { ok: false, errors } : { ok: true, data };
}

export function validateSpec(raw: unknown): ValidationResult<z.infer<typeof SpecSchema>> {
  const parsed = SpecSchema.safeParse(raw);
  if (!parsed.success) return { ok: false, errors: zodErrors(parsed.error) };
  return { ok: true, data: parsed.data };
}

/**
 * Load + validate routes.json. On failure prints the errors and exits non-zero
 * (fail-closed): a malformed policy file must never silently route. Used by the
 * CLI and the regression runner.
 */
export function loadRoutesOrExit(path: string): Routes {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'));
  } catch (e: any) {
    console.error(`✗ Could not read/parse the routing allowlist at ${path}: ${e?.message ?? e}`);
    console.error('  routes.json is the single source of truth. It must exist and be valid JSON.');
    process.exit(3);
  }
  const res = validateRoutes(raw);
  if (!res.ok) {
    console.error(
      `✗ routes.json failed policy validation (${res.errors.length} error${res.errors.length === 1 ? '' : 's'}):`,
    );
    for (const e of res.errors) console.error(`    - ${e}`);
    console.error('  Fix routes.json before any delegation can run (fail-closed).');
    process.exit(3);
  }
  return res.data;
}
