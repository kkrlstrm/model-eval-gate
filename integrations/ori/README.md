# Ori Eval adapter — measure there, govern here

> [Ori Eval](https://openrouter.ai/blog/announcements/ori-eval) picks the winner.
> model-eval-gate decides whether there should be one — and whether that is still true next month.

This is a different kind of integration from [`openclaw/`](../openclaw/) and
[`hermes/`](../hermes/). Those are **runtime** adapters: they ask the gate at call time.
This one is a **tooling** adapter: it moves work between an eval product and this
repo's policy, in both directions, and it enforces nothing at runtime.

## Why an adapter and not a competitor

This repo's README has always said it is [not an eval framework](../../README.md) — that it
consumes eval decisions and keeps them honest. Ori is now the most likely such tool a user
of this repo will already have: it ships from OpenRouter, installs in one command, and is
good at precisely the half this repo deliberately does not do.

|  | Ori Eval | model-eval-gate |
|---|---|---|
| runs a real **agent loop** | ✅ | ❌ — single model call, by design |
| asserts over the **trajectory** (which tools ran) | ✅ | ✅ *against a recorded one* |
| **pass^k** consistency across k trials | ❌ | ✅ |
| **provider pin** so prod runs the scored endpoint | ❌ | ✅ |
| `do_not_use_when` — the negative constraint | ❌ | ✅ |
| **refusal** as an outcome | ❌ every table has a winner | ✅ |
| subscription vs API **counterfactual** | ❌ reports `$/PR` | ✅ |

Neither list is a criticism. They are different jobs, and the seams line up.

## Adapt or run standalone

**Nothing here requires Ori to be installed.** `emit` writes a text file; `import` reads a
JSONL file; `status` reports either way and exits 0. If Ori is absent, every spec in
`eval/regression/` is still fully runnable by this repo's own harness, trajectory graders
included — they score against a trajectory **recorded in the spec** rather than one observed
live. What you lose without Ori is the ability to *produce* a fresh trajectory, not the
ability to assert on one.

```bash
npm run ori status          # is Ori usable here, and what works if it isn't
```

## Direction 1 — `emit`: a governed spec → a runnable Ori eval

```bash
npm run ori emit -- --mode filter-auto-reply                 # print
npm run ori emit -- --mode support-triage --out .            # write evals/<mode>/<mode>.regression.eval.ts
npm run ori emit -- --mode support-triage --shape bakeoff --limit 5 --max-prompt-price 0.000005
```

- **`regression`** (default) pins the mode's **currently allowlisted** model and wraps it in
  `assertModelIsLive()`, so the eval fails loudly if the catalog drops it. Repoint
  `routes.json` and the emitted file must be regenerated — a stale pin would measure a model
  nobody approved.
- **`bakeoff`** runs the same tasks against live `candidateModels()`. A bake-off produces a
  **proposal**, never an approval.

**Two refusals worth knowing about**, both there to stop the adapter from manufacturing
false confidence:

1. **Unmapped graders are reported, not approximated.** `fieldAgreement`, `enumMatch`,
   `numericWithinTolerance` and friends are deterministic gold comparisons that Ori's
   run-level assertion API does not express. They are listed in the generated file's header
   as *not asserted here*, and they stay enforced by `eval/regression.ts`. An eval that
   silently checks something weaker than its spec is worse than one that admits the gap.
2. **A vacuous emit is refused.** If every grader in a spec is unmappable, all that survives
   is `run.toComplete()` — green whenever the agent returns anything at all. The CLI exits
   non-zero rather than writing that, because a file like that reads as coverage. Override
   with `--allow-empty` and it is written with a warning banner.

## Direction 2 — `import`: an Ori run → a proposal

```bash
npm run ori import -- --mode support-triage                                  # reads .ori/eval/history.jsonl
npm run ori import -- --mode support-triage --history path/to/history.jsonl --write
```

It emits three things: a per-model summary, a **frozen regression spec**, and a
`routes.json` mode **stub**. It exits non-zero when the proposal is not fit to act on, so CI
cannot merge it blind.

**It never writes `routes.json`.** Ori's documented scheduled workflow is: re-run monthly,
and when a model scores better than the incumbent, open a PR you merge. For a coding agent's
default model that is a good default. For a *governed mode* it is the exact failure this repo
exists to prevent — a model swap merged on a single run, with no negative constraint, no
pass^k check, no provider pin, and no dated record of what was replaced or why.

So the stub ships **deliberately incomplete**. `purpose`, `use_when` and `do_not_use_when`
are `TODO`. The last one is the point: the negative constraint comes from a human reading the
failures, and no eval tool can infer it. Auto-filling it would manufacture the confidence the
gate is supposed to withhold.

The importer also reports what the run **does not** establish — pass^k, a provider pin, and
the subscription counterfactual (if the work is subscription-billed, the dollar column is the
cost of *leaving*, not a saving).

### Reading `history.jsonl` defensively

Ori's per-record shape is not documented, so `normalizeRecord()` probes several plausible
field names, leaves anything it cannot find **absent rather than defaulted**, and skips —
counting — any record with no attributable model. Unparseable lines are surfaced as a
blocker. A partial read is never reported as a clean one.

Consequences worth stating: a trial whose pass/fail cannot be determined is **not** counted
as a pass (the rate is over *decided* trials only), and if Ori changes its schema the
importer degrades to "I could not attribute these records" instead of inventing a verdict.

## Trajectory graders

The concept borrowed from Ori, in this repo's idiom. Six new grader kinds:

| kind | asserts |
|---|---|
| `toolCalled` | every named tool appears in the trajectory |
| `toolNotCalled` | none of the named tools appears — the destructive-action guard |
| `toolSequence` | named tools appear in this relative order |
| `mentions` | the output mentions each substring (inverse of `mustNotContain`) |
| `costAtMost` | this trial cost ≤ N USD |
| `latencyAtMost` | this trial finished within N ms |

**Missing evidence fails.** With no trajectory recorded, `toolCalled` does not pass ("we
never saw it call the tool") and `toolNotCalled` does not pass either ("we cannot prove it
didn't"). This is the same rule the gate applies to absent caller metadata: unchecked is not
satisfied. `eval/regression.ts` warns up front when a spec declares a trajectory grader but
no task carries a trajectory, so the failure reads as a missing recording rather than a model
regression.

A spec graded this way should set `"output_format": "text"` — an agent's answer is prose, and
the JSON parser would otherwise register a parse failure on every trial and report it as
drift.

```jsonc
{
  "mode": "support-triage",
  "output_format": "text",
  "graders": [
    { "kind": "toolCalled",    "tools": ["lookup_order"] },
    { "kind": "toolNotCalled", "tools": ["issue_refund", "delete_file"] },
    { "kind": "costAtMost",    "maxUsd": 0.02 }
  ],
  "tasks": [
    {
      "id": "refund-01",
      "vars": { "prompt": "A customer wants a refund for order #1234." },
      "gold": {},
      "trajectory": [{ "name": "lookup_order" }]   // recorded from a real run
    }
  ]
}
```

## Tests

`test/graders_ori.ts` (wired into `npm test`) covers the graders, the emitter and the
importer offline — no key, no network. The load-bearing cases are the negative ones: absent
trajectory must not pass, a single-trial win must be blocked as evidence, `do_not_use_when`
must stay a TODO, and a vacuous emit must be flagged.
