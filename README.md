# model-eval-gate

**Eval-gated model routing with an enforced allowlist.** Don't route a task to a cheaper model until a recorded eval proves that model is safe for that *exact* task type — and enforce it at the CLI, not by convention.

Most "model routers" pick a model at call time on price or vibes ("use the cheap one here"). `model-eval-gate` inverts that: a task can only be delegated to a non-frontier model through a **named mode**, and a mode only exists because an eval against your real data cleared a strict quality bar. Everything else is refused. Retired modes return a refusal with the reason and the date. The routing table is a *governed policy*, not a lookup.

```
$ npx tsx src/cli.ts translate "..."
✗ Mode "translate" is not on the allowlist.
  model-eval-gate enforces a strict routing policy (see GOVERNANCE.md).
  Only eval-verified modes are allowed; everything else is refused here.

$ npx tsx src/cli.ts generic-cheap "..."
✗ Mode "generic-cheap" is RETIRED.
  Retired 2026-05-18. A mode named for a model instead of a use case. Too generic;
  it invited misuse on single-row decisions. Replaced by the narrower extract-bulk.
```

## Why

Cheap models are genuinely good at *some* things and quietly terrible at others, and the boundary is task-shaped, not model-shaped. A model that's at frontier parity on bulk field extraction can be 20% wrong on the same extraction when a human acts on each row. The only way to know where the line is, is to run an eval on your own data — and the only way to keep that knowledge from rotting is to (a) encode the boundary where it can't be ignored and (b) re-check it as models drift.

`model-eval-gate` is the thin layer that does both:

- **Modes are named for the use case, not the model** (`extract-bulk`, `filter-auto-reply`), so you can't pattern-match a model to a task type — you have to describe the task.
- **Each mode carries `use_when` / `do_not_use_when`** tied to a documented failure, so misuse is hard.
- **The allowlist is enforced at the CLI/library layer**, not left to a prompt or a code review.
- **A regression harness** re-checks each mode against the model the allowlist currently routes it to, over multiple trials, and fails on drift.

It is **not** an eval framework (use Promptfoo / Braintrust / Harbor to *run* evals). It's the governance layer that sits on top of eval results and **enforces the verdict**.

## Install

```bash
git clone https://github.com/<you>/model-eval-gate && cd model-eval-gate
npm install
cp .env.example .env      # add your OPENROUTER_API_KEY
```

Needs Node 18+ and an [OpenRouter](https://openrouter.ai) key. `model-eval-gate` is OpenRouter-native (one key, many models).

## Use

**As a CLI** (the enforcer):

```bash
npx tsx src/cli.ts help                          # the current allowlist, with verified dates
npx tsx src/cli.ts extract-bulk "<prompt>"       # runs — allowed mode
npx tsx src/cli.ts extract-bulk --stdin "Extract fields:" < big.txt
npx tsx src/cli.ts extract-multimodal "Read this roster" --image page.png
npx tsx src/cli.ts anything-else "..."           # refused
```

**From Python** (the same allowlist, no Node required):

```python
from src.router import text_call, vision_call
text, usd = text_call("digest-longcontext", "Summarize the key people.", long_document)
text, usd = vision_call("extract-multimodal", "Read the phone numbers.", "page.png")
# an off-allowlist mode raises ValueError — same refusal as the CLI
```

## The allowlist — `routes.json`

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

## Three things that make this more than a config file

### 1. Provider pinning (eval → prod drift guard)

OpenRouter routes a model id to varying underlying providers and quantizations; an eval scored on one endpoint isn't guaranteed to be what production hits. ([Anthropic quantified how much infrastructure alone can swing agentic eval scores.](https://www.anthropic.com/engineering/infrastructure-noise)) Each mode takes an optional `provider` pin (`{order, only, allowFallbacks, quantizations}`) that both the CLI and the Python port pass on every call, so a production call runs on the endpoint the eval was scored on. The regression harness captures the served provider so you can set a pin from real data.

### 2. A regression harness with pass@k / pass^k

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
routes.json              the allowlist — single source of truth
src/cli.ts               the CLI enforcer (Node/tsx)
src/router.py            the Python port — same allowlist, stdlib + requests
eval/harness.ts          k-trial runner: pass@k / pass^k, cost, served provider
eval/graders.ts          code / model / human graders
eval/regression.ts       re-run frozen specs, detect drift, exit non-zero
eval/regression/*.json   frozen specs + synthetic gold
GOVERNANCE.md            the policy: a mode requires a passing eval
docs/ADDING_A_MODE.md ·  docs/OBSERVATIONS.example.md
```

## License

MIT © 2026 Kai Karlstrom
