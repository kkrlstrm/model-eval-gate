/**
 * `ori-adapter` CLI — the two directions, plus the standalone check.
 *
 *   emit    a governed spec  → a runnable Ori *.eval.ts
 *   import  an Ori run       → a PROPOSAL (spec + mode stub). Never routes.json.
 *   status  is Ori usable here, and what still works if it isn't
 *
 * ADAPT OR RUN STANDALONE. Nothing here requires Ori to be installed. `emit`
 * writes a text file; `import` reads a JSONL file; `status` reports and exits 0
 * either way. If Ori is absent, every spec in eval/regression/ is still fully
 * runnable by this repo's own harness. Ori is an optional second opinion that
 * covers the agent-loop half this harness does not do — not a dependency.
 *
 * Usage:
 *   npx tsx integrations/ori/cli.ts status
 *   npx tsx integrations/ori/cli.ts emit --mode filter-auto-reply [--shape bakeoff] [--out DIR]
 *   npx tsx integrations/ori/cli.ts import --mode my-new-mode [--history .ori/eval/history.jsonl] [--write DIR]
 */
import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { loadRoutesOrExit, validateSpec } from '../../src/schema.ts';
import { emitOriEval, type EmitShape } from './emit.ts';
import { summarizeHistory, proposeModeStub, proposeSpec, renderProposal } from './import.ts';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..', '..');
const SPEC_DIR = join(ROOT, 'eval', 'regression');

const argv = process.argv.slice(2);
const cmd = argv[0];
const flag = (n: string): string | undefined => {
  const i = argv.indexOf(n);
  return i >= 0 ? argv[i + 1] : undefined;
};
const has = (n: string) => argv.includes(n);
const today = new Date().toISOString().slice(0, 10);

/** Is the `ori` CLI on PATH? Absence is a supported state, not an error. */
function oriStatus(): { installed: boolean; version?: string } {
  try {
    const v = execFileSync('ori', ['--version'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    return { installed: true, version: v.trim() };
  } catch {
    return { installed: false };
  }
}

function findSpecForMode(mode: string): { path: string; spec: any } {
  if (!existsSync(SPEC_DIR)) fail(`no spec directory at ${SPEC_DIR}`);
  for (const f of readdirSync(SPEC_DIR).filter((f) => f.endsWith('.json'))) {
    const p = join(SPEC_DIR, f);
    const raw = JSON.parse(readFileSync(p, 'utf8'));
    if (raw?.mode === mode) {
      const v = validateSpec(raw);
      if (!v.ok) fail(`spec ${p} is invalid:\n  - ${v.errors.join('\n  - ')}`);
      return { path: p, spec: raw };
    }
  }
  fail(
    `no regression spec for mode "${mode}" in ${SPEC_DIR}.\n` +
      `  A mode with no spec is a verdict nobody re-measures — write the spec first (docs/ADDING_A_MODE.md).`,
  );
}

function fail(msg: string): never {
  console.error(`✗ ${msg}`);
  process.exit(2);
}

// ── status ──────────────────────────────────────────────────────────────────
if (cmd === 'status' || !cmd) {
  const st = oriStatus();
  console.log(st.installed ? `✓ ori installed (${st.version})` : `· ori not installed`);
  console.log(
    st.installed
      ? `  Both directions available: emit a spec to an Ori eval, import an Ori run as a proposal.`
      : `  This is fine. emit/import are pure file operations and still work.\n` +
          `  What you lose without Ori: running the agent loop to PRODUCE a trajectory.\n` +
          `  What still works: every spec in eval/regression/ via \`npx tsx eval/regression.ts\`,\n` +
          `  including trajectory graders scored against a recorded trajectory in the spec.`,
  );
  const specs = existsSync(SPEC_DIR)
    ? readdirSync(SPEC_DIR).filter((f) => f.endsWith('.json'))
    : [];
  console.log(`  ${specs.length} regression spec(s) available to emit.`);
  process.exit(0);
}

// ── emit ────────────────────────────────────────────────────────────────────
if (cmd === 'emit') {
  const mode = flag('--mode') ?? fail('emit needs --mode <name>');
  const shape = (flag('--shape') ?? 'regression') as EmitShape;
  if (shape !== 'regression' && shape !== 'bakeoff') fail(`--shape must be regression|bakeoff`);

  const routes = loadRoutesOrExit(join(ROOT, 'routes.json'));
  const { spec } = findSpecForMode(mode);
  const modeDef = routes.modes?.[mode];
  if (!modeDef && shape === 'regression') {
    console.log(
      `⚠ "${mode}" is not on the allowlist — emitting against the workspace default model.\n` +
        `  A passing run will NOT make it a mode; see docs/ADDING_A_MODE.md.`,
    );
  }

  const limit = flag('--limit');
  const maxPrice = flag('--max-prompt-price');
  const out = emitOriEval(spec, modeDef, {
    shape,
    candidateLimit: limit ? Number(limit) : undefined,
    maxPromptPrice: maxPrice ? Number(maxPrice) : undefined,
  });

  // An emitted file whose only surviving assertion is `toComplete()` would read as
  // coverage while checking nothing. Refuse to write it unless asked explicitly.
  if (out.vacuous && !has('--allow-empty')) {
    console.error(
      `✗ refusing to emit a vacuous eval for "${mode}".\n` +
        `  Every grader in this spec is a deterministic gold-comparison with no Ori\n` +
        `  equivalent, so the generated file would assert only run.toComplete() —\n` +
        `  green whatever the model says. That is worse than no file.\n\n` +
        `  Options:\n` +
        `    - add a trajectory/mentions/budget grader to the spec, then re-emit\n` +
        `    - keep enforcing this mode here: npx tsx eval/regression.ts --mode ${mode}\n` +
        `    - --allow-empty to write it anyway (it carries a warning banner)`,
    );
    process.exit(2);
  }

  const outDir = flag('--out');
  if (outDir) {
    const full = join(resolve(outDir), out.filename);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, out.source);
    console.log(`✓ wrote ${full}`);
  } else {
    console.log(out.source);
  }

  if (out.unmapped.length) {
    console.error(
      `\n⚠ ${out.unmapped.length} grader(s) had no faithful Ori equivalent and were NOT emitted:\n` +
        out.unmapped.map((u) => `    - ${u}`).join('\n') +
        `\n  They remain enforced here: npx tsx eval/regression.ts --mode ${mode}\n` +
        `  (Approximating them in the emitted file would silently weaken the check.)`,
    );
  }
  if (!oriStatus().installed && outDir) {
    console.log(
      `\n· ori is not installed, so the file above cannot be run yet.\n` +
        `  Install: curl -fsSL https://openrouter.ai/labs/ori/install.sh | bash && ori login`,
    );
  }
  process.exit(0);
}

// ── import ──────────────────────────────────────────────────────────────────
if (cmd === 'import') {
  const mode = flag('--mode') ?? fail('import needs --mode <name>');
  const historyPath = resolve(flag('--history') ?? '.ori/eval/history.jsonl');

  let summary;
  try {
    summary = summarizeHistory(historyPath);
  } catch (e: any) {
    fail(String(e?.message ?? e));
  }

  const routes = loadRoutesOrExit(join(ROOT, 'routes.json'));
  if (routes.retired?.[mode]) {
    fail(
      `"${mode}" is RETIRED (${routes.retired[mode].reason}).\n` +
        `  A retired mode is not revived by a good score — that is the whole point of a dated retirement.\n` +
        `  Propose it under a new name, with a new eval.`,
    );
  }

  const { stub, blockers } = proposeModeStub(summary, today);
  const spec = proposeSpec(mode, summary, today);
  const specPath = join(SPEC_DIR, `${mode}.imported.json`);

  const writeDir = has('--write');
  if (writeDir) {
    mkdirSync(SPEC_DIR, { recursive: true });
    writeFileSync(specPath, JSON.stringify(spec, null, 2) + '\n');
    console.log(`✓ wrote proposed spec → ${specPath}\n`);
  }

  console.log(
    renderProposal(
      mode,
      summary,
      stub,
      blockers,
      writeDir ? specPath : '(not written; pass --write)',
    ),
  );
  console.log(
    `\nNOTHING was written to routes.json. This is a proposal.\n` +
      `A better score is not a permission — a human fills in do_not_use_when and applies it.`,
  );
  // Non-zero when the proposal is not fit to act on, so CI can't merge it blind.
  process.exit(blockers.length ? 1 : 0);
}

fail(`unknown command "${cmd}" — expected status | emit | import`);
