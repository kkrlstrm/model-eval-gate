/**
 * `check` — validate all policy files OFFLINE (no API calls, no key needed).
 * Run it in CI and pre-commit. It validates routes.json and every regression
 * spec structurally, cross-checks that each spec targets a real mode with known
 * grader kinds, and reports staleness + modes lacking regression coverage.
 *
 *   npx tsx src/check.ts
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { validateRoutes, validateSpec } from './schema.ts';
import { GRADER_KINDS } from '../eval/graders.ts';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const ROUTES_PATH = join(ROOT, 'routes.json');
const SPEC_DIR = join(ROOT, 'eval', 'regression');

const errors: string[] = [];
const warnings: string[] = [];
const readJson = (p: string) => JSON.parse(readFileSync(p, 'utf8'));

// ── routes.json ──
let modeNames: string[] = [];
let staleDays = 0;
let modes: Record<string, any> = {};
try {
  const raw = readJson(ROUTES_PATH);
  const res = validateRoutes(raw);
  if (!res.ok) {
    for (const e of res.errors) errors.push(`routes.json: ${e}`);
  } else {
    modes = res.data.modes;
    modeNames = Object.keys(modes);
    staleDays = res.data.staleness_warn_days ?? 0;
    console.log(
      `✓ routes.json — ${modeNames.length} modes, ${Object.keys(res.data.retired).length} retired`,
    );
    // staleness
    if (staleDays) {
      const now = Date.now();
      for (const [n, m] of Object.entries(modes)) {
        const age = Math.floor((now - Date.parse((m as any).verified_date)) / 86_400_000);
        if (age > staleDays)
          warnings.push(
            `mode "${n}" verified ${age}d ago (> ${staleDays}d) — re-run its regression eval`,
          );
      }
    }
  }
} catch (e: any) {
  errors.push(`routes.json: could not read/parse — ${e?.message ?? e}`);
}

// ── regression specs ──
const covered = new Set<string>();
if (existsSync(SPEC_DIR)) {
  for (const f of readdirSync(SPEC_DIR).filter((f) => f.endsWith('.json'))) {
    const p = join(SPEC_DIR, f);
    let raw: any;
    try {
      raw = readJson(p);
    } catch (e: any) {
      errors.push(`${f}: unparseable — ${e?.message ?? e}`);
      continue;
    }
    const res = validateSpec(raw);
    if (!res.ok) {
      for (const e of res.errors) errors.push(`${f}: ${e}`);
      continue;
    }
    const spec = res.data;
    covered.add(spec.mode);
    if (modeNames.length && !(spec.mode in modes))
      errors.push(`${f}: targets mode "${spec.mode}" which is not in routes.json modes`);
    for (const g of spec.graders) {
      if (!GRADER_KINDS.has((g as any).kind))
        errors.push(
          `${f}: unknown grader kind "${(g as any).kind}" (known: ${[...GRADER_KINDS].join(', ')})`,
        );
    }
    console.log(
      `✓ ${f} — mode "${spec.mode}", ${spec.tasks.length} tasks, ${spec.graders.length} grader(s)`,
    );
  }
}
for (const n of modeNames) {
  if (!covered.has(n))
    warnings.push(
      `mode "${n}" has no regression spec — its verified permission isn't being re-checked`,
    );
}

// ── report ──
console.log('');
for (const w of warnings) console.log(`⚠ ${w}`);
if (errors.length) {
  console.error(`\n✗ ${errors.length} policy error${errors.length === 1 ? '' : 's'}:`);
  for (const e of errors) console.error(`    - ${e}`);
  process.exit(1);
}
console.log(
  `\n✓ policy OK${warnings.length ? ` (${warnings.length} warning${warnings.length === 1 ? '' : 's'})` : ''}.`,
);
process.exit(0);
