# Routing governance

This is the policy `model-eval-gate` enforces. It governs when a task may be delegated to a non-frontier model versus handled by the orchestrator (your primary/frontier model) directly.

## The default is the orchestrator

The orchestrator handles all work by default. Delegating to a cheaper worker is the **exception**, not the norm. Before delegating, the orchestrator must satisfy the gate:

1. The task fits one of the allowed modes **literally** — not a similar-sounding task type.
2. The `use_when` criteria are met **as written**.
3. None of the `do_not_use_when` conditions apply.

If any of those fail, the orchestrator handles the task directly. **Cost savings do not override the quality bar.** For per-call, low-volume, or customer-facing work, the frontier model is usually cheaper *in expected value* once you account for re-do work and the cost of a missed error.

## Modes are named for the use case, not the model

`extract-bulk`, not `qwen`. Invocation is a description of the task, not a model pick. This is deliberate: naming a mode after a model invites pattern-matching a model to a task type ("structured data → the cheap one"), which is exactly how cheap models get misused on the 20% of cases where they quietly fail. If your mode name is a model name, you've built a lookup table, not a policy.

## What stays with the orchestrator (typical)

These rarely have a safe cheap-model mode:

- **Drafting** anything customer-facing (entity-hallucination risk; voice/context lives with the orchestrator).
- **Single-row classification with downstream consequences** (a 10–20% disagreement rate becomes a real error rate when each row triggers an action).
- **Primary architecture or security review** (cheaper models surface complementary findings but miss the load-bearing ones).
- **Positioning / messaging critique** (the structurally-important insight is the one the cheap model misses).
- **Real-time / web-connected research** (use a first-class web search tool with the orchestrator).
- **Anything where an operator acts directly on a single output.**

Cheap models earn a mode when the volume is high, the quality bar genuinely tolerates some noise (aggregate analytics, binary gating, a supplemental lens), and the orchestrator does the gating and synthesis around the cheap call.

## Deciding *what* to eval (before you eval anything)

The steps below assume you already know which task shape is a candidate. Usually you
don't, and the model catalog cannot tell you — it does not know what you do. Work
therefore enters this process from **observed telemetry**, not from a price list:

1. **Observe.** `meg observe ingest` reads the Claude Code and Codex sessions you have
   already run; `meg observe sync` pulls the provider's spend rollup. Only the *shape*
   of a call is stored — never prompt or argument content.
2. **Cluster.** `meg workload list` groups calls into workload classes and marks each
   eligible or not. **Eligibility is a property of the task, not the price.** A class is
   blocked when volume is too low to eval meaningfully (< 30 observations), when the work
   is subscription-billed (§ *The counterfactual*), or when stakes are high **or
   unconfirmed** — unknown stakes are treated as high, never as low.
3. **Propose.** `meg workload propose <class>` applies capability requirements as **hard
   filters first** (structured outputs, modality, context), then ranks survivors by
   projected spend **at that class's measured token profile**. List price is not the
   ranking key; a model with a higher list price can be cheaper on your actual token mix.
   Free tiers and auto-router pseudo-models are excluded by default — a free endpoint
   rate-limits hard enough that an eval scored on it does not predict production, and an
   auto-router picks a different model at call time, so the thing you measured is not the
   thing that runs.
4. **Scaffold.** `meg eval scaffold <class>` emits a spec for a human to review. It is
   never auto-run: the stakes question cannot be derived from telemetry, and a pipeline
   that guesses it would be guessing about whether a person acts on a single row.

A slate is not a verdict. It is the list of things worth measuring.

## The counterfactual: what a win actually buys

Work that already runs on a subscription has a marginal cost of roughly **$0**. Moving it
to a cheaper model therefore saves **no money at all** — it buys rate-limit headroom and
throughput, which are real benefits that must not be laundered into a dollar figure.

So `billing` is recorded per call, and every counterfactual states which case it is. A
saving is only claimed for work that would otherwise be **API-billed**. Reporting a
subscription "saving" is a governance failure, not a rounding difference: it manufactures
a justification for moving work that had no cost problem.

## Adding a mode

A new mode requires, in order:

1. **An eval against your real data** (not synthetic), using the harness pattern in `eval/`.
   The prompt must be **imported verbatim from production**, not paraphrased — a
   paraphrase measures a task you do not run.
2. **A validated grader, before any candidate spend.** Run `meg.scaffold.validate()` with
   known-good and known-bad examples. A grader that does not separate them is rejected:
   it will produce confident, uniformly-bad numbers that read as a finding about the
   models when they are a finding about the grader. Prefer a **code** grader wherever the
   output has a checkable contract; use a model panel only for generative output.
3. **Cross-family judges, if a panel is used.** At least two judges from *different* model
   families. A same-family judge inflates its own arm, and the self-preference delta and
   inter-judge agreement must be reported by default, not on request. A single-judge panel
   is not a verdict.
4. **Quality scored against a frontier anchor** (your orchestrator model) on that data.
5. **A clear pass of a strict bar** — measurable parity for the use case, not "good enough with caveats." If the recommendation needs an "if you scaffold the prompt with…" clause, it does not qualify.
6. **A `use_when` / `do_not_use_when` pair** narrow enough that misuse is hard, each tied to something the eval actually showed. Where the boundary is machine-checkable, also add a **`constraints`** block (`min_rows`, `forbid_single_row_decision`, `requires_human_review`, `allowed_input_types`, `max_stakes`) so the gate enforces task eligibility, not just the mode name.
7. **An entry in `routes.json`** (+ an evidence entry in your observations log) with a `verified_date`. `routes.json` is validated on load — a malformed policy file (missing field, non-ISO date, generic mode name, a name in both `modes` and `retired`) fails closed and refuses everything. Run `npm run check` to validate offline.

A mode that later fails a re-eval is **retired with a dated reason**, not silently deleted — the refusal message teaches the next operator why.

See [docs/ADDING_A_MODE.md](docs/ADDING_A_MODE.md) for the step-by-step, and [docs/OBSERVATIONS.example.md](docs/OBSERVATIONS.example.md) for the evidence-trail pattern.

## Keeping verdicts honest over time

- **Verified-date staleness.** Each mode carries `verified_date`; the CLI warns when a verdict is older than `staleness_warn_days`. A months-old "this model is fine" is a hypothesis, not a fact.
- **Regression.** Every passing eval graduates into a frozen regression spec (`eval/regression/*.json`) that re-runs against the model the allowlist currently routes the mode to, over k trials, and fails on drift. This catches a model degrading, an OpenRouter provider change, or a routes edit that swapped the model out from under a mode.
- **Provider pinning.** Because the same model id can be served by different providers/quantizations, pin `provider` on a mode once you know which endpoint your eval was scored on, so production can't silently drift onto a worse one.
- **Verify the artifact, not only the endpoint.** Re-running an eval proves the model still
  behaves; it does not prove the data already in your database is sound. Stored output has
  been measured scoring *worse* than a fresh run of the same model on the same prompt. A
  regression that only pings the endpoint will not see that.
- **Dead modes are a finding.** A mode nobody calls, or a model called by nobody's mode, is
  drift. `meg observe coverage` and per-mode call counts surface both — a flagship mode
  receiving one request in thirty days while its traffic quietly migrated to an
  unevaluated sibling is exactly the state this policy exists to make visible.

## Graduated response, and posture

A gate with only "yes" and "no" forces two bad choices: block work you are not yet sure
about, or wave it through silently. Four actions instead:

| action | delegates? | meaning |
|---|---|---|
| `allow` | yes | earned, and every declared constraint was evidenced |
| `monitor` | yes | recorded, nothing enforced — how a new mode is rolled onto live work |
| `nudge` | yes | proceeds, and hands back *why it is questionable* (unverified constraint, stale verdict) |
| `refuse` | no | the mode exists, this task does not qualify |
| `block` | no | unknown mode, retired mode, or unreadable policy |

**`nudge` is the one that earns adoption.** In an agent runtime the note becomes context the
model reads and self-corrects on: free when the model was right, and it saves a bad call
when it was not.

**Posture decides what an *unproven* condition means** — the only place reasonable people
differ. `attended` (a human reads the output) nudges on an unchecked constraint or a stale
verdict. `unattended` (cron, fleet worker, an agent loop at 3am) refuses, because there
"nobody objected" is not evidence. Choose it once at the integration boundary rather than
per call site.

## Every decision is recorded, and the record is tamper-evident

Every claim this policy makes is retrospective: *that workload was refused*, *this mode was
allowed under these constraints*, *nothing bypassed the gate last month*. Console logging
supports none of them.

`meg/audit.py` appends one hash-chained line per decision, so editing or removing an earlier
line breaks the chain from that point and `verify()` reports where. It does not stop someone
truncating the file — that is what an external witness is for — but it makes *silent edits*
detectable, and the realistic threat is a decision quietly reclassified after something went
wrong, not one deleted outright.

Writing is best-effort by design: an audit failure must never block a call. A gate that goes
down because its logger's disk filled has failed closed on availability grounds, which is
worse than a gap in the log — and the gap is itself visible, because the chain records a
sequence.

## The repository's own claims are gated too

A project that gates delegation on evidence should not gate its own promises on good
intentions. Both run in CI:

- `gates/verify_no_real_data.py` — fails when a tracked file holds a credential or real
  telemetry (managed-service hostnames, absolute home paths). Documentation placeholders are
  allowlisted **by exact string, never by loosening a pattern** — a relaxed DSN regex would
  let a real credential through, which is the wrong side of that trade.
- `gates/verify_doc_refs.py` — fails when a doc points at a file, a mode, or a CLI
  subcommand that does not exist. These docs are instructions an agent will act on; a dead
  path makes it improvise the thing the helper existed to prevent.

Both also run at **commit time** via `.githooks/pre-commit` (`npm run hooks:install`). CI
catches a leaked credential before it merges; it does not stop it existing, and a key in a
commit needs a rotation rather than an edit. The hook scans the **index**, not the working
tree, because those differ in both directions — staging a key and then fixing it on disk
without re-adding leaves a clean tree and a dirty commit.

Two limits, stated rather than papered over: `git commit --no-verify` bypasses the hook and
git gives a hook no way to observe its own bypass, and a missing interpreter makes the hook
skip rather than block. CI is the backstop; the hook shortens the loop, it is not the
boundary.

## Enforcement has a boundary — know where it is

This is a fail-closed gate **for calls that pass through it**. It is not a sandbox or a
network policy boundary: code that calls a provider directly bypasses it entirely, and that
is not hypothetical — it is the most common way an allowlist rots. Two defences:

- **Make the gate the only egress path.** Run calling code without provider keys in its
  environment and expose only the recorder/router.
- **Measure the bypass.** `meg observe coverage` compares recorded calls against the
  provider's own request count. A gap means something reached the provider around the gate.
  Treat a persistent gap as a policy violation, not a telemetry nuisance.

Related: never hardcode a model id at a call site. Resolve it from `routes.json` at call
time. Documented model choices drift from executed ones silently — a single call site has
been observed naming three *different* models across its docstring, an inline comment, and
its actual constant.

## Presets are hypotheses

Modes shipped in this repository were verified on someone else's data. Inheriting them is
not the same as earning them: run the mode's regression spec on your own data before
relying on it, and treat any mode without a spec as "measured once, by someone else,
elsewhere." Shipping presets as verdicts would violate rule 1 of *Adding a mode*.
