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

## Adding a mode

A new mode requires, in order:

1. **An eval against your real data** (not synthetic), using the harness pattern in `eval/`.
2. **Quality scored against a frontier anchor** (your orchestrator model) on that data.
3. **A clear pass of a strict bar** — measurable parity for the use case, not "good enough with caveats." If the recommendation needs an "if you scaffold the prompt with…" clause, it does not qualify.
4. **A `use_when` / `do_not_use_when` pair** narrow enough that misuse is hard, each tied to something the eval actually showed.
5. **An entry in `routes.json`** (+ an evidence entry in your observations log) with a `verified_date`.

A mode that later fails a re-eval is **retired with a dated reason**, not silently deleted — the refusal message teaches the next operator why.

See [docs/ADDING_A_MODE.md](docs/ADDING_A_MODE.md) for the step-by-step, and [docs/OBSERVATIONS.example.md](docs/OBSERVATIONS.example.md) for the evidence-trail pattern.

## Keeping verdicts honest over time

- **Verified-date staleness.** Each mode carries `verified_date`; the CLI warns when a verdict is older than `staleness_warn_days`. A months-old "this model is fine" is a hypothesis, not a fact.
- **Regression.** Every passing eval graduates into a frozen regression spec (`eval/regression/*.json`) that re-runs against the model the allowlist currently routes the mode to, over k trials, and fails on drift. This catches a model degrading, an OpenRouter provider change, or a routes edit that swapped the model out from under a mode.
- **Provider pinning.** Because the same model id can be served by different providers/quantizations, pin `provider` on a mode once you know which endpoint your eval was scored on, so production can't silently drift onto a worse one.
