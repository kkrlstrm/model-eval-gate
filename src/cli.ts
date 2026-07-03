/**
 * model-eval-gate — the ENFORCER.
 *
 * Delegates a single task to a non-frontier model via OpenRouter, but ONLY for a
 * mode that a recorded eval proved is safe for that exact task shape, AND only if
 * the task metadata satisfies that mode's constraints. Every request whose mode
 * isn't on the allowlist is refused before any model call. Retired modes return a
 * refusal with the reason and date. This is delegation as governed policy, not vibes.
 *
 * NOTE ON SCOPE: this is a fail-closed gate for calls that GO THROUGH it. It is not
 * a sandbox or a network policy boundary — an agent that calls OpenRouter directly
 * bypasses it. To make it a real control plane, make this the only model-egress path.
 *
 * The allowlist lives in `routes.json` (validated on load by src/schema.ts, shared
 * with the Python port in src/router.py). Read GOVERNANCE.md before adding a mode.
 *
 *   npx tsx src/cli.ts <mode> <prompt> [--rows N] [--stakes low|medium|high]
 *                                      [--input text|image] [--single-row] [--human-reviewed]
 *   npx tsx src/cli.ts explain <mode>
 *   npx tsx src/cli.ts help
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { OpenRouter } from '@openrouter/sdk';
import { callModel } from '@openrouter/agent';
import { loadRoutesOrExit, type ModeDef } from './schema.ts';
import { canDelegate, type TaskMeta } from './preflight.ts';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROUTES_PATH = join(HERE, '..', 'routes.json');
const ROUTES = loadRoutesOrExit(ROUTES_PATH); // fail-closed on a malformed policy file

const MODES = ROUTES.modes;
const RETIRED_MODES: Record<string, string> = Object.fromEntries(
  Object.entries(ROUTES.retired ?? {}).map(([k, v]) => [
    k,
    `Retired ${v.retired_date ?? ''}. ${v.reason}`.replace(/\s+/g, ' ').trim(),
  ]),
);
const STALE_DAYS = ROUTES.staleness_warn_days ?? 0;

function daysSince(isoDate?: string): number | null {
  if (!isoDate) return null;
  const t = Date.parse(isoDate);
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86_400_000);
}

// ─────────────────────────────────────────────────────────────────────────────
// CLI parsing
// ─────────────────────────────────────────────────────────────────────────────
const mode = (process.argv[2] ?? '').toLowerCase().trim();

function printHelp(): void {
  console.log('\nUsage: npx tsx src/cli.ts <mode> <prompt>');
  console.log('       npx tsx src/cli.ts explain <mode>');
  console.log(
    '\nAllowed modes (each backed by a recorded eval — see GOVERNANCE.md; data in routes.json):\n',
  );
  for (const [name, m] of Object.entries(MODES)) {
    const age = daysSince(m.verified_date);
    const stale = STALE_DAYS && age !== null && age > STALE_DAYS ? '  ⚠ STALE' : '';
    console.log(`  ${name}${stale}`);
    console.log(`      model:        ${m.model}`);
    console.log(`      purpose:      ${m.purpose}`);
    console.log(`      use when:     ${m.use_when}`);
    console.log(`      do NOT use:   ${m.do_not_use_when}`);
    if (m.verified_date)
      console.log(`      verified:     ${m.verified_date}${age !== null ? ` (${age}d ago)` : ''}`);
    if (m.provider) console.log(`      provider pin: ${JSON.stringify(m.provider)}`);
    if (m.constraints) console.log(`      constraints:  ${JSON.stringify(m.constraints)}`);
    console.log('');
  }
  console.log(
    'Task-eligibility flags: --rows N --stakes low|medium|high --input text|image --single-row --human-reviewed',
  );
  console.log('Any other mode is refused. New modes require a passing eval per GOVERNANCE.md.');
}

function printExplain(name: string): number {
  const m = MODES[name];
  if (!m) {
    if (name in RETIRED_MODES) {
      console.log(`${name}: RETIRED — ${RETIRED_MODES[name]}`);
      return 0;
    }
    console.error(`✗ "${name}" is not a known mode. Run \`help\` for the allowlist.`);
    return 2;
  }
  const age = daysSince(m.verified_date);
  const stale = STALE_DAYS && age !== null && age > STALE_DAYS;
  console.log(`\n${name}`);
  console.log(`  model:          ${m.model}`);
  console.log(`  purpose:        ${m.purpose}`);
  console.log(`  use when:       ${m.use_when}`);
  console.log(`  do NOT use:     ${m.do_not_use_when}`);
  console.log(`  evidence:       ${m.evidence_ref ?? '(none recorded)'}`);
  console.log(
    `  verified:       ${m.verified_date}${age !== null ? ` (${age}d ago)` : ''}${stale ? '  ⚠ STALE — re-run its regression eval' : ''}`,
  );
  console.log(
    `  provider pin:   ${m.provider ? JSON.stringify(m.provider) : 'none (router picks — eval→prod drift possible)'}`,
  );
  console.log(
    `  constraints:    ${m.constraints ? JSON.stringify(m.constraints) : 'none (task eligibility is advisory only)'}`,
  );
  console.log(`  pricing:        $${m.priceIn}/M in · $${m.priceOut}/M out\n`);
  return 0;
}

if (!mode || mode === '-h' || mode === '--help' || mode === 'help') {
  printHelp();
  process.exit(mode ? 0 : 1);
}
if (mode === 'explain') {
  process.exit(printExplain((process.argv[3] ?? '').trim()));
}

// flag + prompt parsing
const rawArgs = process.argv.slice(3);
const imagePaths: string[] = [];
const meta: TaskMeta = {};
const rest: string[] = [];
let useStdin = false;
for (let i = 0; i < rawArgs.length; i++) {
  const a = rawArgs[i];
  switch (a) {
    case '--image': {
      const p = rawArgs[++i];
      if (p) imagePaths.push(p);
      break;
    }
    case '--stdin':
      useStdin = true;
      break;
    case '--rows':
      meta.rows = Number(rawArgs[++i]);
      break;
    case '--stakes':
      meta.stakes = rawArgs[++i] as any;
      break;
    case '--input':
      meta.input_type = rawArgs[++i] as any;
      break;
    case '--single-row':
      meta.single_row_decision = true;
      break;
    case '--human-reviewed':
      meta.human_review = true;
      break;
    default:
      rest.push(a);
  }
}
if (imagePaths.length && meta.input_type == null) meta.input_type = 'image';
let prompt = rest.join(' ').trim();
if (useStdin) {
  let piped = '';
  try {
    piped = readFileSync(0, 'utf8');
  } catch {}
  if (piped.trim()) prompt = (prompt ? prompt + '\n\n' : '') + piped;
}

if (mode in RETIRED_MODES) {
  console.error(`✗ Mode "${mode}" is RETIRED.`);
  console.error(`  ${RETIRED_MODES[mode]}`);
  console.error('\nRun `src/cli.ts help` for the current allowlist.');
  process.exit(2);
}
if (!(mode in MODES)) {
  console.error(`✗ Mode "${mode}" is not on the allowlist.\n`);
  console.error('  model-eval-gate enforces a strict delegation policy (see GOVERNANCE.md).');
  console.error(
    '  Only eval-verified modes are allowed; everything else stays with the orchestrator',
  );
  console.error(
    '  (your frontier model). If your task does not fit an earned mode, handle it there.\n',
  );
  console.error('Run `src/cli.ts help` for the allowlist.');
  process.exit(2);
}
if (!prompt) {
  console.error(`✗ Mode "${mode}" requires a prompt as remaining args.`);
  process.exit(1);
}

const m: ModeDef = MODES[mode];

// Preflight: enforce task eligibility, not just the mode name.
const pf = canDelegate(m, meta);
if (!pf.ok) {
  console.error(
    `✗ Mode "${mode}" is on the allowlist, but the task does not satisfy its constraints:`,
  );
  for (const v of pf.violations) console.error(`    - ${v}`);
  console.error(
    '  This is the boundary between an eval-earned permission and misuse. Handle it with the orchestrator.',
  );
  process.exit(2);
}
if (pf.unchecked.length) {
  console.error(
    `⚠ Mode "${mode}" declares constraints not covered by the metadata you passed (allowed, but unverified):`,
  );
  for (const u of pf.unchecked) console.error(`    - ${u}`);
}

console.log(`Mode:    ${mode}`);
console.log(`Model:   ${m.model}`);
console.log(`Purpose: ${m.purpose}`);
const age = daysSince(m.verified_date);
if (STALE_DAYS && age !== null && age > STALE_DAYS) {
  console.error(
    `⚠ Mode "${mode}" was last verified ${age}d ago (> ${STALE_DAYS}d). Re-run its regression eval; the model may have drifted.`,
  );
}
console.log('───');

const client = new OpenRouter({ apiKey: process.env.OPENROUTER_API_KEY });

let modelInput: any = prompt;
if (imagePaths.length) {
  const content: any[] = [{ type: 'input_text', text: prompt }];
  for (const ip of imagePaths) {
    const b64 = readFileSync(ip).toString('base64');
    const mime = /\.jpe?g$/i.test(ip) ? 'image/jpeg' : 'image/png';
    content.push({ type: 'input_image', imageUrl: `data:${mime};base64,${b64}`, detail: 'high' });
  }
  modelInput = [{ role: 'user', content }];
  console.log(`Images:  ${imagePaths.length} attached`);
  console.log('───');
}

const callArgs: any = { model: m.model, input: modelInput };
if (m.provider) callArgs.provider = m.provider;

const t0 = Date.now();
try {
  const result = callModel(client, callArgs);
  const text = await result.getText();
  const ms = Date.now() - t0;
  let usage: any = null;
  let served: string | null = null;
  try {
    const resp = (await result.getResponse()) as any;
    usage = resp?.usage ?? null;
    served = resp?.provider ?? resp?.raw?.provider ?? null;
  } catch {}
  const inTok = usage?.inputTokens ?? usage?.prompt_tokens ?? 0;
  const outTok = usage?.outputTokens ?? usage?.completion_tokens ?? 0;
  const estCost = (inTok * m.priceIn + outTok * m.priceOut) / 1_000_000;
  // Prefer OpenRouter's reported cost; fall back to the route-file estimate.
  const apiCost = typeof usage?.cost === 'number' ? usage.cost : null;
  const cost = apiCost ?? estCost;
  console.log(text);
  console.log('───');
  const costStr =
    apiCost != null ? `$${apiCost.toFixed(4)} (reported)` : `$${estCost.toFixed(4)} (est)`;
  const provStr = served
    ? ` · via ${served}${m.provider ? ' (pinned)' : ''}`
    : m.provider
      ? ` · pin ${JSON.stringify(m.provider)}`
      : '';
  console.log(`✓ ${(ms / 1000).toFixed(1)}s · ${inTok}in / ${outTok}out · ${costStr}${provStr}`);
  // Warn if the route-file price estimate diverges materially from reality.
  if (apiCost != null && estCost > 0 && Math.abs(apiCost - estCost) / apiCost > 0.25) {
    console.error(
      `⚠ route-file price estimate ($${estCost.toFixed(4)}) diverges >25% from reported ($${apiCost.toFixed(4)}) — update priceIn/priceOut for "${mode}".`,
    );
  }
  process.exit(0);
} catch (err: any) {
  const msg = err?.error?.message ?? err?.message ?? String(err);
  const code = err?.error?.code ?? err?.status ?? '';
  console.error(`✗ ${m.model}${code ? ` [${code}]` : ''}: ${msg}`);
  process.exit(1);
}
