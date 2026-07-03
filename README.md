# model-eval-gate

**A circuit breaker for cheap-model delegation.**

Agents shouldn't route work to a cheaper model just because it's cheap, fast, or "probably good enough." A non-frontier model gets used only when it has *earned* a narrow permission: a named task mode, backed by an eval on your real data, with explicit `use_when` / `do_not_use_when` boundaries. Everything else stays with the frontier / orchestrator model.

**model-eval-gate turns eval results into enforceable delegation policy.** If a task matches an eval-verified mode, it can run on the approved non-frontier model. If it doesn't, the call is refused. Retired modes fail closed with the date and the reason, so old shortcuts don't silently come back.

```
$ npx tsx src/cli.ts translate "..."
✗ Mode "translate" is not on the allowlist.
  model-eval-gate enforces a strict delegation policy (see GOVERNANCE.md).
  Only eval-verified modes are allowed; everything else stays with the orchestrator.

$ npx tsx src/cli.ts generic-cheap "..."
✗ Mode "generic-cheap" is RETIRED.
  Retired 2026-05-18. A mode named for a model instead of a use case. Too generic;
  it invited misuse on single-row decisions. Replaced by the narrower extract-bulk.
```

## The failure mode

The dangerous delegation bug isn't an outage. It's a **quiet downgrade.**

A cheap model works beautifully on 80% of a task shape, gets generalized into "use this for extraction / classification / summarization," and then starts handling the 20% where it fails silently. Nobody notices until a human acts on a single wrong row, a customer-facing draft hallucinates an entity, or quality slips because the provider behind a model ID changed underneath you.

model-eval-gate prevents that failure mode by making delegation **explicit, narrow, evidenced, and reversible.**

## Why this isn't a router

"Model routing" is the wrong comparison set. LiteLLM / OpenRouter route the *call* — provider selection, cost and latency, fallback. model-eval-gate sits one layer up and decides whether the call is **allowed to be delegated at all.**

> OpenRouter can route the call.
> model-eval-gate decides whether the call is allowed to be delegated in the first place.

Three ideas do the work:

1. **Refusal is the feature.** Most routers optimize "where should this go?" This says: unless a task has earned a mode, it doesn't go anywhere cheaper. Off-allowlist and retired modes are refused, not silently downgraded.
2. **Modes are named for the task, not the model.** `extract-bulk`, never `qwen`. A mode named after a model becomes a vibes-based lookup table; a mode named after a task shape forces you to describe what you're actually doing — and makes misuse obvious.
3. **Eval verdicts become production policy.** `routes.json` is the single source of truth — purpose, `use_when`, `do_not_use_when`, evidence reference, verified date, provider pin — read live by both the CLI and the Python port.

## Not an eval framework

model-eval-gate doesn't replace your eval platform (Promptfoo, Braintrust, Harbor, your own harness). It **consumes eval decisions and keeps them honest:**

- an **initial eval** decides whether a mode may exist;
- a **regression spec** checks whether that permission is still valid.

The included harness is policy *maintenance*, not a competing eval product.

## The lifecycle

```
   candidate cheap model
          │
          ▼
   eval on your real task data
          │
          ▼
   mode earns a narrow permission
          │
          ▼
   routes.json   ← the allowlist / single source of truth
          │
          ▼
   CLI + Python enforce it   ← refuse everything off-allowlist
          │
          ▼
   regression re-checks for drift
          │
     ┌────┴─────┐
   pass        fail
   keep mode   retire with a dated reason
```

## Install

```bash
git clone https://github.com/kkrlstrm/model-eval-gate && cd model-eval-gate
npm install
cp .env.example .env      # add your OPENROUTER_API_KEY
```

Needs Node 18+ and an [OpenRouter](https://openrouter.ai) key. model-eval-gate is OpenRouter-native (one key, many models).

## Use

**As a CLI** (the enforcer):

```bash
npx tsx src/cli.ts help                          # the current allowlist, with verified dates
npx tsx src/cli.ts extract-bulk "<prompt>"       # runs — this task has an earned mode
npx tsx src/cli.ts extract-bulk --stdin "Extract fields:" < big.txt
npx tsx src/cli.ts extract-multimodal "Read this roster" --image page.png
npx tsx src/cli.ts anything-else "..."           # refused — no earned mode
```

**From Python** (the same allowlist, no Node required):

```python
from src.router import text_call, vision_call
text, usd = text_call("digest-longcontext", "Summarize the key people.", long_document)
text, usd = vision_call("extract-multimodal", "Read the phone numbers.", "page.png")
# an off-allowlist mode raises ValueError — same refusal as the CLI
```

## `routes.json` — eval verdicts as policy

One file is the source of truth, read live by both the CLI and the Python port:

```jsonc
{
  "modes": {
    "extract-bulk": {
      "model": "qwen/qwen3-235b-a22b-2507",
      "purpose": "High-volume field extraction whose output feeds aggregate analytics.",
      "use_when": "N > 50 rows AND output is counted/grouped/distributed (not acted on per-row).",
      "do_not_use_when": "An operator reads a single output row and decides from it. ~20% disagreement with the frontier anchor on judgment-laden fields.",
      "evidence_ref": "docs/OBSERVATIONS.example.md 'Eval 1'",
      "verified_date": "2026-05-18",
      "provider": null
    }
  },
  "retired": { "generic-cheap": { "retired_date": "2026-05-18", "reason": "..." } }
}
```

The six modes shipped here are **realistic examples** to show the shape. Replace them with modes your own evals justify. See **[GOVERNANCE.md](GOVERNANCE.md)** for the policy and **[docs/ADDING_A_MODE.md](docs/ADDING_A_MODE.md)** for the workflow.

## Three things that keep the policy honest

### 1. Provider pinning — the eval→prod drift guard

Passing an eval against `model-x` isn't enough if production might hit a different provider, quantization, or serving stack. OpenRouter routes a model ID across providers by uptime and cost unless you say otherwise, and [Anthropic has shown that infrastructure configuration alone can swing agentic eval scores by several points](https://www.anthropic.com/engineering/infrastructure-noise) — sometimes more than the gap between models on a leaderboard. Each mode takes an optional `provider` pin (`{order, only, allowFallbacks, quantizations}`) that both the CLI and the Python port pass on every call, so **production runs against the thing you actually tested.** The regression harness records the served provider so you can set a pin from real data.

### 2. Regression with pass@k / pass^k

Capability evals ("can it do this?") graduate into regression evals ("does it still?"). `eval/regression.ts` re-runs each frozen spec against **the model the allowlist currently routes that mode to** (so a swapped model is caught), over **k trials**, and reports:

- **pass@k** — did at least one of k trials pass (shots on goal)
- **pass^k** — did *all* k trials pass (consistency; the metric that matters for binary-gating / customer-facing modes)
- score + stdev, cost, latency, served provider

It flags **score drift**, **consistency drift**, and **model swap**, and exits non-zero — wire it into CI or a scheduled job.

```bash
npx tsx eval/regression.ts                    # run every spec
npx tsx eval/regression.ts --mode filter-auto-reply --k 5
npx tsx eval/regression.ts --update-baseline  # record current numbers as the new baseline
```

Specs live in `eval/regression/*.json` and ship with a **synthetic corpus** (fabricated leads / email replies with unambiguous gold) so you can `git clone` and run a real regression today. Swap in your own gold sets.

### 3. A three-grader taxonomy

`eval/graders.ts` implements the [three grader kinds from Anthropic's evals guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents): **code** (deterministic, the default), **model** (LLM-as-judge, opt-in), **human** (a recorded verdict frozen into the spec). A spec declares which graders apply; the harness combines them.

## Layout

```
routes.json              the allowlist — eval verdicts as enforceable policy
src/cli.ts               the CLI enforcer (Node/tsx) — refuses everything off-allowlist
src/router.py            the Python port — same allowlist, stdlib + requests
eval/harness.ts          k-trial runner: pass@k / pass^k, cost, served provider
eval/graders.ts          code / model / human graders
eval/regression.ts       re-run frozen specs, detect drift, exit non-zero
eval/regression/*.json   frozen specs + synthetic gold
GOVERNANCE.md            the policy: a mode requires a passing eval
docs/ADDING_A_MODE.md ·  docs/OBSERVATIONS.example.md
```

## License

Apache-2.0 © 2026 Kai Karlstrom
