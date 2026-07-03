/**
 * model-eval-gate — the ENFORCER.
 *
 * Delegates a single task to a non-frontier model via OpenRouter, but ONLY for a
 * mode that a recorded eval proved is safe for that exact task shape. It is not a
 * general-purpose model picker: every request whose mode isn't on the allowlist
 * is refused at the CLI layer. Retired modes return a refusal with the reason and
 * date. This is delegation as governed policy, not vibes.
 *
 * The allowlist lives in `routes.json` (the single source of truth, shared with
 * the Python port in src/router.py). This file reads it and enforces it. Read
 * GOVERNANCE.md before adding a mode — new modes require a passing eval.
 *
 *   npx tsx src/cli.ts <mode> <prompt>
 *   npx tsx src/cli.ts help
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { OpenRouter } from '@openrouter/sdk';
import { callModel } from '@openrouter/agent';

// OpenRouter provider-routing preference (a subset of ProviderPreferences).
// Pinning this makes a production call run on the same underlying endpoint /
// quantization the eval was scored on — closes eval->prod drift.
type ProviderPin = {
  order?: string[];
  only?: string[];
  allowFallbacks?: boolean;
  quantizations?: string[];
};

type Mode = {
  model: string;
  priceIn: number;
  priceOut: number;
  purpose: string;
  use_when: string;
  do_not_use_when: string;
  evidence_ref?: string;
  verified_date?: string;
  provider?: ProviderPin | null;
};

type Routes = {
  staleness_warn_days?: number;
  modes: Record<string, Mode>;
  retired: Record<string, { retired_date?: string; reason: string }>;
};

// ─────────────────────────────────────────────────────────────────────────────
// Load the allowlist from routes.json (canonical, at the repo root).
// ─────────────────────────────────────────────────────────────────────────────
const HERE = dirname(fileURLToPath(import.meta.url));
const ROUTES_PATH = join(HERE, '..', 'routes.json');

let ROUTES: Routes;
try {
  ROUTES = JSON.parse(readFileSync(ROUTES_PATH, 'utf8')) as Routes;
} catch (e: any) {
  console.error(`✗ Could not load the routing allowlist at ${ROUTES_PATH}: ${e?.message ?? e}`);
  console.error('  routes.json is the single source of truth for allowed modes. It must exist.');
  process.exit(3);
}

const MODES: Record<string, Mode> = ROUTES.modes ?? {};
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
// CLI
// ─────────────────────────────────────────────────────────────────────────────

function printHelp(): void {
  console.log('');
  console.log('Usage: npx tsx src/cli.ts <mode> <prompt>');
  console.log('');
  console.log('Allowed modes (each backed by a recorded eval — see GOVERNANCE.md; data in routes.json):');
  console.log('');
  for (const [name, m] of Object.entries(MODES)) {
    const age = daysSince(m.verified_date);
    const stale = STALE_DAYS && age !== null && age > STALE_DAYS ? '  ⚠ STALE' : '';
    console.log(`  ${name}${stale}`);
    console.log(`      model:        ${m.model}`);
    console.log(`      purpose:      ${m.purpose}`);
    console.log(`      use when:     ${m.use_when}`);
    console.log(`      do NOT use:   ${m.do_not_use_when}`);
    if (m.verified_date) console.log(`      verified:     ${m.verified_date}${age !== null ? ` (${age}d ago)` : ''}`);
    if (m.provider) console.log(`      provider pin: ${JSON.stringify(m.provider)}`);
    console.log('');
  }
  console.log('Any other mode is refused. New modes require a passing eval per GOVERNANCE.md.');
}

const mode = (process.argv[2] ?? '').toLowerCase().trim();
// --stdin: append piped content to the prompt. Lets a caller pass a long page /
// document on stdin instead of as a shell arg (ARG_MAX). Backward-compatible.
// --image <path>: attach an image (repeatable). Used by a vision mode to send a
// rendered page image to a multimodal model. Non-image modes ignore it.
const rawArgs = process.argv.slice(3);
const imagePaths: string[] = [];
const argsNoImg: string[] = [];
for (let i = 0; i < rawArgs.length; i++) {
  if (rawArgs[i] === '--image') { const p = rawArgs[++i]; if (p) imagePaths.push(p); }
  else argsNoImg.push(rawArgs[i]);
}
const useStdin = argsNoImg.includes('--stdin');
let prompt = argsNoImg.filter((a) => a !== '--stdin').join(' ').trim();
if (useStdin) {
  let piped = '';
  try { piped = readFileSync(0, 'utf8'); } catch {}
  if (piped.trim()) prompt = (prompt ? prompt + '\n\n' : '') + piped;
}

if (!mode || mode === '-h' || mode === '--help' || mode === 'help') {
  printHelp();
  process.exit(mode ? 0 : 1);
}

if (mode in RETIRED_MODES) {
  console.error(`✗ Mode "${mode}" is RETIRED.`);
  console.error(`  ${RETIRED_MODES[mode]}`);
  console.error('');
  console.error('Run `src/cli.ts help` for the current allowlist.');
  process.exit(2);
}

if (!(mode in MODES)) {
  console.error(`✗ Mode "${mode}" is not on the allowlist.`);
  console.error('');
  console.error('  model-eval-gate enforces a strict delegation policy (see GOVERNANCE.md).');
  console.error('  Only eval-verified modes are allowed; everything else stays with the orchestrator');
  console.error('  (your frontier model). If your task does not fit an earned mode, handle it there.');
  console.error('');
  console.error('Run `src/cli.ts help` for the allowlist.');
  process.exit(2);
}

if (!prompt) {
  console.error(`✗ Mode "${mode}" requires a prompt as remaining args.`);
  process.exit(1);
}

const m = MODES[mode];
console.log(`Mode:    ${mode}`);
console.log(`Model:   ${m.model}`);
console.log(`Purpose: ${m.purpose}`);
// Staleness advisory: a mode whose eval verdict is older than the configured
// window should be re-evaluated. Non-fatal — the call still runs.
const age = daysSince(m.verified_date);
if (STALE_DAYS && age !== null && age > STALE_DAYS) {
  console.error(`⚠ Mode "${mode}" was last verified ${age}d ago (> ${STALE_DAYS}d). Re-run its regression eval; the model may have drifted.`);
}
console.log('───');

const client = new OpenRouter({ apiKey: process.env.OPENROUTER_API_KEY });

// Multimodal: build a message array with the prompt + each image as an input_image
// content block (OpenAI Responses style). Text-only modes pass the bare string.
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

// Provider pinning: if the mode declares a provider preference, pass it through so
// this call runs on the same underlying endpoint/quantization the eval was scored on.
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
    // Which provider actually served the request (OpenRouter echoes this).
    served = resp?.provider ?? resp?.raw?.provider ?? null;
  } catch {}
  const inTok = usage?.inputTokens ?? usage?.prompt_tokens ?? 0;
  const outTok = usage?.outputTokens ?? usage?.completion_tokens ?? 0;
  const cost = (inTok * m.priceIn + outTok * m.priceOut) / 1_000_000;
  console.log(text);
  console.log('───');
  const provStr = served ? ` · via ${served}${m.provider ? ' (pinned)' : ''}` : (m.provider ? ` · pin ${JSON.stringify(m.provider)}` : '');
  console.log(`✓ ${(ms / 1000).toFixed(1)}s · ${inTok}in / ${outTok}out · $${cost.toFixed(4)}${provStr}`);
  process.exit(0);
} catch (err: any) {
  const msg = err?.error?.message ?? err?.message ?? String(err);
  const code = err?.error?.code ?? err?.status ?? '';
  console.error(`✗ ${m.model}${code ? ` [${code}]` : ''}: ${msg}`);
  process.exit(1);
}
