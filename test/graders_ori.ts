/**
 * Offline tests for the trajectory/budget graders and the Ori adapter.
 *
 * The load-bearing assertions here are the NEGATIVE ones. A trajectory grader
 * that silently passes when no trajectory was recorded would let a spec advertise
 * behavioural coverage it never had — the same class of bug as a gate treating
 * absent metadata as satisfied. Several tests below exist only to pin that down.
 *
 * No network, no API key. Run with: npx tsx test/graders_ori.ts
 */
import {
  buildGrader,
  GRADER_KINDS,
  TRAJECTORY_GRADER_KINDS,
  type GradeCtx,
} from '../eval/graders.ts';
import { emitOriEval } from '../integrations/ori/emit.ts';
import {
  normalizeRecord,
  proposeModeStub,
  type OriRunSummary,
} from '../integrations/ori/import.ts';

let failed = 0;
function ok(name: string, cond: boolean, detail = '') {
  if (cond) console.log(`  ✓ ${name}`);
  else {
    console.error(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
    failed++;
  }
}

const ctx = (over: Partial<GradeCtx> = {}): GradeCtx => ({ gold: {}, ...over });
const grade = async (cfg: any, parsed: any, c: GradeCtx) => await buildGrader(cfg).grade(parsed, c);

console.log('trajectory graders — evidence present');
{
  const traj = [{ name: 'lookup_order' }, { name: 'issue_refund' }];
  const called = await grade(
    { kind: 'toolCalled', tools: ['lookup_order'] },
    {},
    ctx({ trajectory: traj }),
  );
  ok('toolCalled passes when the tool was called', called.score === 1);

  const missing = await grade(
    { kind: 'toolCalled', tools: ['verify_identity'] },
    {},
    ctx({ trajectory: traj }),
  );
  ok('toolCalled fails when the tool was never called', missing.score === 0);

  const forbidden = await grade(
    { kind: 'toolNotCalled', tools: ['issue_refund'] },
    {},
    ctx({ trajectory: traj }),
  );
  ok('toolNotCalled fails when the forbidden tool WAS called', forbidden.score === 0);

  const clean = await grade(
    { kind: 'toolNotCalled', tools: ['delete_file'] },
    {},
    ctx({ trajectory: traj }),
  );
  ok('toolNotCalled passes when the forbidden tool was not called', clean.score === 1);

  const seq = await grade(
    { kind: 'toolSequence', sequence: ['lookup_order', 'issue_refund'] },
    {},
    ctx({ trajectory: traj }),
  );
  ok('toolSequence passes in the right order', seq.score === 1);

  const badSeq = await grade(
    { kind: 'toolSequence', sequence: ['issue_refund', 'lookup_order'] },
    {},
    ctx({ trajectory: traj }),
  );
  ok('toolSequence fails when the order is wrong (refund before lookup)', badSeq.score === 0);
}

console.log('trajectory graders — evidence ABSENT must not pass');
{
  const called = await grade({ kind: 'toolCalled', tools: ['x'] }, {}, ctx());
  ok('toolCalled FAILS with no trajectory (cannot prove it was called)', called.score === 0);
  ok(
    '…and says why',
    called.assertions.every((a) => /unverifiable/.test(a.detail ?? '')),
    JSON.stringify(called.assertions),
  );

  const notCalled = await grade({ kind: 'toolNotCalled', tools: ['delete_file'] }, {}, ctx());
  ok(
    'toolNotCalled ALSO fails with no trajectory (cannot prove it was not called)',
    notCalled.score === 0,
  );

  const seq = await grade({ kind: 'toolSequence', sequence: ['a', 'b'] }, {}, ctx());
  ok('toolSequence fails with no trajectory', seq.score === 0);
}

console.log('trajectory graders do not require parseable JSON');
{
  // An agent's answer is prose. A trajectory assertion must still be scorable.
  const r = await grade(
    { kind: 'toolCalled', tools: ['search'] },
    null,
    ctx({ trajectory: [{ name: 'search' }], text: 'I looked it up for you.' }),
  );
  ok('toolCalled scores against a null parse when the trajectory is present', r.score === 1);
  ok(
    '…and does not emit a spurious parseable assertion',
    !r.assertions.some((a) => a.name === 'parseable'),
  );
}

console.log('mentions / budget graders');
{
  const m = await grade(
    { kind: 'mentions', substrings: ['14-day'] },
    null,
    ctx({ text: 'Our 14-day window applies.' }),
  );
  ok('mentions passes on raw prose text', m.score === 1);

  const m2 = await grade(
    { kind: 'mentions', substrings: ['refund'] },
    null,
    ctx({ text: 'No policy here.' }),
  );
  ok('mentions fails when absent', m2.score === 0);

  const c = await grade(
    { kind: 'costAtMost', maxUsd: 0.01 },
    {},
    ctx({ usage: { costUsd: 0.004 } }),
  );
  ok('costAtMost passes under the cap', c.score === 1);

  const c2 = await grade(
    { kind: 'costAtMost', maxUsd: 0.001 },
    {},
    ctx({ usage: { costUsd: 0.004 } }),
  );
  ok('costAtMost fails over the cap', c2.score === 0);

  const c3 = await grade({ kind: 'costAtMost', maxUsd: 0.01 }, {}, ctx());
  ok('costAtMost fails when cost was never measured', c3.score === 0);

  const l = await grade({ kind: 'latencyAtMost', maxMs: 5000 }, {}, ctx({ usage: { ms: 1200 } }));
  ok('latencyAtMost passes under the cap', l.score === 1);

  const l2 = await grade({ kind: 'latencyAtMost', maxMs: 500 }, {}, ctx({ usage: { ms: 1200 } }));
  ok('latencyAtMost fails over the cap', l2.score === 0);
}

console.log('grader registry');
{
  for (const k of [
    'toolCalled',
    'toolNotCalled',
    'toolSequence',
    'mentions',
    'costAtMost',
    'latencyAtMost',
  ])
    ok(`${k} is registered in GRADER_KINDS`, GRADER_KINDS.has(k));
  ok(
    'TRAJECTORY_GRADER_KINDS covers exactly the trajectory kinds',
    TRAJECTORY_GRADER_KINDS.size === 3,
  );
  let threw = false;
  try {
    buildGrader({ kind: 'toolTeleport' });
  } catch {
    threw = true;
  }
  ok('an unknown grader kind still throws', threw);
}

console.log('ori emitter');
{
  const agenticSpec = {
    mode: 'support-triage',
    title: 'Support triage',
    input_template: '{{q}}',
    output_format: 'text',
    graders: [
      { kind: 'toolCalled', tools: ['lookup_order'] },
      { kind: 'toolNotCalled', tools: ['issue_refund'] },
      { kind: 'costAtMost', maxUsd: 0.02 },
    ],
    tasks: [{ id: 't1', vars: { q: 'refund for #1234?' }, gold: {} }],
  };
  const out = emitOriEval(agenticSpec, { model: 'z/some-model' } as any, {});
  ok('emits a bun:test import', out.source.includes(`from 'bun:test'`));
  ok('emits the ori/eval import', out.source.includes(`from 'ori/eval'`));
  ok(
    'pins the allowlisted model via assertModelIsLive',
    out.source.includes('assertModelIsLive("z/some-model")'),
  );
  ok('translates toolCalled', out.source.includes(`run.tool("lookup_order").toBeCalled();`));
  ok('translates toolNotCalled', out.source.includes(`run.tool("issue_refund").toNotBeCalled();`));
  ok('translates costAtMost', out.source.includes('run.toCostAtMost(0.02)'));
  ok('renders the task prompt into the file', out.source.includes('refund for #1234?'));
  ok('is not vacuous', out.vacuous === false);
  ok('reports no unmapped graders here', out.unmapped.length === 0);

  const goldSpec = {
    mode: 'extract-x',
    title: 'Extraction',
    input_template: '{{a}}',
    graders: [{ kind: 'fieldAgreement', fields: ['x'] }],
    tasks: [{ id: 't1', vars: { a: '1' }, gold: { x: 1 } }],
  };
  const out2 = emitOriEval(goldSpec, undefined, {});
  ok('flags a deterministic gold grader as unmapped', out2.unmapped.includes('fieldAgreement'));
  ok('marks an assertion-free emit as vacuous', out2.vacuous === true);
  ok('vacuous output carries a warning banner', out2.source.includes('ASSERTS ALMOST NOTHING'));

  const bake = emitOriEval(agenticSpec, undefined, { shape: 'bakeoff', candidateLimit: 3 });
  ok('bakeoff shape pulls live candidates', bake.source.includes('candidateModels({'));
  ok('bakeoff honours the limit', bake.source.includes('limit: 3'));
  ok('bakeoff does not pin a single model', !bake.source.includes('assertModelIsLive'));
}

console.log('ori importer — defensive normalization');
{
  ok('reads a flat record', normalizeRecord({ model: 'a/b', passed: true })?.model === 'a/b');
  ok('reads an alternate model key', normalizeRecord({ modelId: 'a/b' })?.model === 'a/b');
  ok('reads a nested model key', normalizeRecord({ meta: { model: 'a/b' } })?.model === 'a/b');
  ok('skips a record with no attributable model', normalizeRecord({ passed: true }) === null);
  ok('skips a blank model', normalizeRecord({ model: '   ' }) === null);
  ok(
    'derives pass from a status string',
    normalizeRecord({ model: 'a/b', status: 'passed' })?.passed === true,
  );
  ok(
    'leaves pass UNDECIDED when nothing says',
    normalizeRecord({ model: 'a/b' })?.passed === undefined,
  );
  ok(
    'normalizes a tool-call trajectory to names',
    JSON.stringify(
      normalizeRecord({ model: 'a/b', toolCalls: [{ name: 'search' }, 'fetch'] })?.tools,
    ) === JSON.stringify(['search', 'fetch']),
  );
}

console.log('ori importer — the proposal refuses to be an approval');
{
  const summary: OriRunSummary = {
    models: [{ model: 'a/b', trials: 1, passed: 1, passRate: 1 }],
    trials: [{ model: 'a/b', passed: true }],
    skipped: 0,
    parseErrors: 0,
    totalRecords: 1,
  };
  const { stub, blockers } = proposeModeStub(summary, '2026-09-01');
  ok(
    'a single-trial win is blocked as evidence',
    blockers.some((b) => /single run|trial/i.test(b)),
  );
  ok('do_not_use_when is left as a REQUIRED todo', /TODO/.test(stub.do_not_use_when));
  ok('use_when is left as a todo', /TODO/.test(stub.use_when));
  ok('provider pin starts unset', stub.provider === null);
  ok('the winning model is carried over', stub.model === 'a/b');

  const dirty: OriRunSummary = { ...summary, parseErrors: 2, skipped: 1 };
  const d = proposeModeStub(dirty, '2026-09-01');
  ok(
    'unparseable history is reported, not ignored',
    d.blockers.some((b) => /did not parse/.test(b)),
  );
  ok(
    'skipped records are reported',
    d.blockers.some((b) => /skipped/.test(b)),
  );

  const empty: OriRunSummary = {
    models: [],
    trials: [],
    skipped: 0,
    parseErrors: 0,
    totalRecords: 0,
  };
  ok('an empty history proposes nothing', proposeModeStub(empty, '2026-09-01').winner === null);
}

console.log(failed ? `\n✗ ${failed} assertion(s) failed` : '\n✓ all graders/ori tests passed');
process.exit(failed ? 1 : 0);
