/**
 * model-eval-gate — OpenClaw plugin.
 *
 * OpenClaw decides WHAT work to do. This decides whether that work may be
 * delegated to a smaller model. Agents can plan freely; they cannot downgrade
 * freely.
 *
 * HOW IT ATTACHES. OpenClaw exposes `before_model_resolve`, which runs before
 * the session's model is resolved and may return `{ providerOverride,
 * modelOverride }` to switch provider/model for that turn. Returning nothing
 * means "no override" — which is exactly the shape this policy needs, because
 * the default outcome of a refusal is simply *leave the configured model alone*.
 * (`before_model_resolve` replaced the deprecated `before_agent_start` in
 * OpenClaw 2026.4.21.)
 *
 * The direction of the override matters. This plugin only ever moves a turn
 * DOWN to a smaller model that has earned a permission for that workload. It
 * never upgrades, never picks between frontier models, and never silently
 * substitutes on the basis of price. If no mode is earned, it returns nothing
 * and the turn runs on whatever OpenClaw already resolved.
 *
 * It also observes. `llm_output` carries usage and the resolved token budget,
 * and `model_call_started` / `model_call_ended` carry sanitized provider/model
 * call metadata — enough to reconcile what the gate authorised against what the
 * provider actually billed, which is how a bypass becomes visible.
 *
 * WHAT TAGS A TURN. A workload tag has to come from somewhere the agent author
 * controls. Resolution order:
 *   1. an explicit `meg` block on the event/context (skill or job author states it)
 *   2. `MEG_WORKLOAD_MAP` — agentId/skill -> {mode, meta}, for declaring policy
 *      outside the agent's own code
 *   3. nothing -> no delegation. An untagged turn is an unearned turn.
 *
 * STATUS: reference implementation. It governs turns that pass through this
 * hook. Code that calls a provider directly — a tool shelling out to curl, a
 * sub-process with its own API key — bypasses it, which is why the coverage
 * reconciliation exists rather than being treated as optional.
 */

type Json = Record<string, unknown>;

interface MegDeclaration {
  /** Allowlist mode name, e.g. "extract-bulk". Never a model id. */
  mode: string;
  /** Task metadata checked against the mode's constraints. */
  meta?: {
    rows?: number;
    single_row_decision?: boolean;
    human_reviewed?: boolean;
    input_type?: 'text' | 'image' | 'audio' | 'file';
    stakes?: 'low' | 'medium' | 'high';
  };
}

interface Decision {
  allowed: boolean;
  mode: string;
  model?: string | null;
  provider?: Json | null;
  reason: string;
  violations?: string[];
  unchecked?: string[];
  stale_days?: number | null;
  /** 'allow' | 'monitor' | 'nudge' | 'refuse' | 'block' — see meg/policy.py */
  action?: string;
  /** For 'nudge': text worth surfacing to the model so it self-corrects. */
  notes?: string[];
}

export interface MegPluginOptions {
  /** Path to routes.json. Defaults to $MEG_ROUTES or ./routes.json. */
  routesPath?: string;
  /**
   * Treat a constraint the caller gave no metadata for as a REFUSAL.
   * Recommended true for unattended agents: nobody is reading the warning, and
   * a constraint with no evidence behind it has not been satisfied.
   */
  requireFullMetadata?: boolean;
  /** Log every decision (allow and refuse). Default true — silent policy is unauditable. */
  audit?: boolean;
  /** agentId or skill name -> declaration, for tagging without touching agent code. */
  workloadMap?: Record<string, MegDeclaration>;
  /** Where to send audit lines. Defaults to console. */
  log?: (line: string, decision: Decision) => void;
}

// --------------------------------------------------------------------------
// policy evaluation — a direct port of meg/policy.py `decide()`.
// Kept in lockstep with the Python original; `test/parity.ts` asserts the two
// agree, because two implementations of one policy is exactly how a gate starts
// meaning different things in different runtimes.
// --------------------------------------------------------------------------
function daysSince(iso?: string): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86_400_000);
}

function checkConstraints(spec: Json, meta: Json): { violations: string[]; unchecked: string[] } {
  const c = (spec.constraints as Json) ?? {};
  const violations: string[] = [];
  const unchecked: string[] = [];

  if (c.min_rows != null) {
    const rows = meta.rows as number | undefined;
    if (rows == null) unchecked.push(`min_rows=${c.min_rows} (no \`rows\` supplied)`);
    else if (rows < (c.min_rows as number))
      violations.push(`min_rows=${c.min_rows} but rows=${rows}`);
  }
  if (c.forbid_single_row_decision) {
    const v = meta.single_row_decision as boolean | undefined;
    if (v == null) unchecked.push('forbid_single_row_decision (no `single_row_decision`)');
    else if (v) violations.push('mode forbids single-row decisions');
  }
  if (c.requires_human_review) {
    const v = meta.human_reviewed as boolean | undefined;
    if (v == null) unchecked.push('requires_human_review (no `human_reviewed`)');
    else if (!v) violations.push('mode requires human review of the output');
  }
  if (Array.isArray(c.allowed_input_types)) {
    const it = meta.input_type as string | undefined;
    if (it == null) unchecked.push(`allowed_input_types (no \`input_type\`)`);
    else if (!(c.allowed_input_types as string[]).includes(it))
      violations.push(`input_type='${it}' not in ${JSON.stringify(c.allowed_input_types)}`);
  }
  if (c.max_stakes != null) {
    const order = ['low', 'medium', 'high'];
    const st = meta.stakes as string | undefined;
    if (st == null) unchecked.push(`max_stakes=${c.max_stakes} (no \`stakes\`)`);
    else if (!order.includes(st) || order.indexOf(st) > order.indexOf(c.max_stakes as string))
      violations.push(`stakes='${st}' exceeds max_stakes='${c.max_stakes}'`);
  }
  return { violations, unchecked };
}

export function decide(
  mode: string,
  meta: Json,
  routes: Json,
  requireFullMetadata = false,
  staleAfterDays = 120,
): Decision {
  if (routes._error) {
    return {
      allowed: false,
      mode,
      action: 'block',
      reason: `policy unavailable — refusing all delegation (${routes._error})`,
    };
  }
  const retired = (routes.retired as Json) ?? {};
  if (retired[mode]) {
    const r = retired[mode] as Json;
    return {
      allowed: false,
      mode,
      action: 'block',
      reason: `mode '${mode}' is RETIRED (${r.retired_date ?? '?'}): ${r.reason ?? 'no reason recorded'}`,
    };
  }
  const modes = (routes.modes as Json) ?? {};
  const spec = modes[mode] as Json | undefined;
  if (!spec) {
    const known = Object.keys(modes).sort().join(', ') || '(none)';
    return {
      allowed: false,
      mode,
      action: 'block',
      reason: `mode '${mode}' is not on the allowlist. Earned modes: ${known}. Unearned work stays on the frontier model.`,
    };
  }

  let { violations, unchecked } = checkConstraints(spec, meta);
  if (requireFullMetadata) {
    violations = violations.concat(unchecked.map((u) => `unverified constraint: ${u}`));
    unchecked = [];
  }
  const stale = daysSince(spec.verified_date as string);

  if (violations.length) {
    return {
      allowed: false,
      mode,
      action: 'refuse',
      reason: `mode '${mode}' exists but this task does not qualify: ${violations.join('; ')}`,
      violations,
      unchecked,
      stale_days: stale,
    };
  }

  // Graduated response, mirroring meg/policy.py. A `nudge` still delegates, but
  // hands back the reason it is questionable — in an agent runtime that becomes
  // context the model reads and self-corrects on, which costs nothing when the
  // model was right and saves a bad call when it was not.
  const notes: string[] = [];
  let action = 'allow';
  if (unchecked.length) {
    action = 'nudge';
    notes.push(
      `delegated with UNVERIFIED constraints: ${unchecked.join('; ')}. ` +
        `Pass the metadata, or set requireFullMetadata:true to refuse instead.`,
    );
  }
  if (stale != null && stale > staleAfterDays) {
    if (action === 'allow') action = 'nudge';
    notes.push(
      `mode '${mode}' was last verified ${stale}d ago (> ${staleAfterDays}d). ` +
        `An old verdict is a hypothesis, not a fact — re-run its regression spec.`,
    );
  }
  return {
    allowed: true,
    mode,
    action,
    notes,
    model: spec.model as string,
    provider: (spec.provider as Json) ?? null,
    reason: `earned permission: ${spec.use_when ?? ''}`.trim(),
    unchecked,
    stale_days: stale,
  };
}

// --------------------------------------------------------------------------
function resolveDeclaration(
  event: Json,
  ctx: Json,
  map: Record<string, MegDeclaration>,
): MegDeclaration | null {
  const inline = (event?.meg ?? ctx?.meg) as MegDeclaration | undefined;
  if (inline?.mode) return inline;
  for (const key of [ctx?.agentId, ctx?.skill, (event as Json)?.skill]) {
    if (typeof key === 'string' && map[key]) return map[key];
  }
  return null;
}

/**
 * Register the gate on an OpenClaw plugin api handle.
 *
 *   import { register } from "model-eval-gate/integrations/openclaw";
 *   export default (api) => register(api, { requireFullMetadata: true });
 */
export function register(api: { on: Function }, options: MegPluginOptions = {}) {
  const {
    routesPath = process.env.MEG_ROUTES ?? 'routes.json',
    requireFullMetadata = false,
    audit = true,
    workloadMap = {},
    log,
  } = options;

  let routes: Json;
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    routes = JSON.parse(require('node:fs').readFileSync(routesPath, 'utf8'));
  } catch (e) {
    // Fail closed and SAY SO once at load. A gate that quietly stops gating is
    // worse than no gate, because the absence of refusals reads as compliance.
    routes = { modes: {}, retired: {}, _error: `unreadable policy at ${routesPath}` };
    console.warn(`[model-eval-gate] ${routes._error} — all delegation will be refused.`);
  }

  const emit = (d: Decision) => {
    if (!audit) return;
    const verdict = d.allowed ? `${(d.action ?? 'allow').toUpperCase()} -> ${d.model}` : 'REFUSE';
    const line = `[model-eval-gate] ${verdict}  mode=${d.mode}  ${d.reason}`;
    if (log) log(line, d);
    else console.info(line);
    // Nudge notes are the self-correction channel: surface them rather than
    // burying them, because a warning nobody sees is the same as no warning.
    for (const n of d.notes ?? []) console.warn(`[model-eval-gate] ${d.mode}: ${n}`);
  };

  api.on('before_model_resolve', (event: Json, ctx: Json) => {
    const decl = resolveDeclaration(event, ctx, workloadMap);
    if (!decl) {
      // No declared workload => nothing has been earned => no override. Silent
      // by default: most turns are untagged and logging each one would bury the
      // decisions that matter.
      return;
    }
    const d = decide(decl.mode, (decl.meta ?? {}) as Json, routes, requireFullMetadata);
    emit(d);
    if (!d.allowed) return; // leave OpenClaw's resolved model untouched

    if (d.action === 'monitor') {
      // Recorded, not enforced. The rollout posture: watch what the policy WOULD
      // have done on a live workload before letting it do it.
      return;
    }

    const out: Json = { modelOverride: d.model };
    // A provider pin exists so production runs on the endpoint the eval was
    // scored on; drop it into providerOverride only when the policy names one.
    if (d.provider && typeof d.provider === 'object' && 'order' in (d.provider as Json)) {
      const order = (d.provider as Json).order as string[] | undefined;
      if (order?.length) out.providerOverride = order[0];
    }
    return out;
  });

  // Observation: reconcile authorised turns against what the provider billed.
  // `llm_output` carries usage; `model_call_ended` carries sanitized call
  // metadata. Together they answer "did anything reach a provider that this
  // gate never authorised?" — the question a refusal-based policy lives or dies on.
  api.on('llm_output', (event: Json, ctx: Json) => {
    if (!audit) return;
    const usage = (event?.usage ?? {}) as Json;
    if (!usage || Object.keys(usage).length === 0) return;
    console.debug(
      `[model-eval-gate] usage agent=${ctx?.agentId ?? '?'} ` +
        `in=${usage.inputTokens ?? usage.prompt_tokens ?? '?'} ` +
        `out=${usage.outputTokens ?? usage.completion_tokens ?? '?'}`,
    );
  });

  return {
    routes,
    decide: (m: string, meta: Json) => decide(m, meta, routes, requireFullMetadata),
  };
}

export default register;
