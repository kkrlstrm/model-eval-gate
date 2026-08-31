# model-eval-gate

<!-- portfolio-status -->
**Status:** Reference implementation — extracted from a private production GTM system; tenant data, provider adapters, and company-specific policy stay private. · **Layer:** Quality & policy enforcement · **[Portfolio map ›](https://github.com/kkrlstrm)**

**Cost-first routers tell you what's cheap. This tells you what's *safe to move* — and what isn't.**

Agents shouldn't route work to a cheaper model just because it's cheap, fast, or "probably good enough." A non-frontier model gets used only when it has *earned* a narrow permission: a named task mode, backed by an eval on your real data, with explicit `use_when` / `do_not_use_when` boundaries. Everything else stays with the frontier / orchestrator model.

It starts from **the work you already do** — read out of your Claude Code and Codex sessions — not from a price list. See [Where the modes come from](#where-the-modes-come-from-v2).

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

## Where the modes come from (v2)

v1 assumed you already knew which task shapes to eval. Most people don't — and the model
catalog can't tell you, because the catalog doesn't know what you do. So v2 adds the half
that comes *before* the gate:

```
observe ──▶ cluster ──▶ propose ──▶ scaffold ──▶ validate ──▶ run ──▶ gate ──▶ maintain
   │           │           │           │            │          │        │         │
harness     workload    candidate     eval       grader     bake-off  routes  regression
adapters    classes     slate from    spec +     sanity     vs        .json   + staleness
+ call                  the live      samples    check      control           + dead-mode
recorder                catalog                                                feedback
```

Every other tool in this space starts at the catalog and asks *"what's cheap?"*. Starting
from observed work makes the first question *"what do I actually do, and which parts are
even eligible to move?"* — and eligibility is a property of the task, not the price list.

```bash
meg observe ingest        # read Claude Code + Codex sessions you have ALREADY run
meg observe sync          # pull the provider's daily spend rollup
meg observe coverage      # is anything reaching the provider around the gate?
meg workload list         # what work exists, and what may move
meg workload propose supplier-page-digest    # candidates ranked on YOUR token profile
meg eval scaffold supplier-page-digest       # a reviewable eval spec
```

See it end-to-end on invented data — no setup, no key:

```bash
python3 examples/fictional_workload.py
```

### Telemetry: both harnesses, one schema

Two coding agents, two mechanisms, because they expose telemetry differently and pretending
otherwise loses data:

| harness | mechanism | why |
|---|---|---|
| **Claude Code** | lifecycle HTTP hooks + local transcript JSONL | hooks fire for every tool; the transcript path also works **retroactively**, so you get a workload picture from past sessions without having configured anything first |
| **Codex** | tail the append-only rollout JSONL under `~/.codex/sessions/**` | Codex hooks fire only for shell commands, so a hook-based reader silently misses edits, MCP calls and sub-agents |

Both land in one `work_events` table, so everything downstream is harness-agnostic. This
mirrors the split proven by [cc-logger](https://github.com/kkrlstrm/cc-logger) and
[codex-logger](https://github.com/kkrlstrm/codex-logger).

**Only the *shape* of a call is stored, never its content.** Commands are reduced to an
argv0 and a script name, prompts to a length and a fingerprint. These databases get shared
when someone asks for routing help; prompts and arguments would carry customer data and
credentials into that conversation.

### The counterfactual nobody else computes

If a call runs inside a Claude Code or Codex session on a subscription, the frontier
model's marginal cost is **$0**. Routing it elsewhere saves nothing — it buys rate-limit
headroom and throughput, which are real but are not dollars. The saving is real only for
work that would otherwise be **API-billed**.

So `billing` is recorded per call, and the counterfactual says so out loud:

```
supplier-page-digest   frontier $420.18 → candidate $11.81   ratio 35.6x
                       DOLLARS SAVED $408.37   ← real saving: this work is API-billed

claim-summary-draft    frontier  $12.55 → candidate  $0.29   ratio 43.5x
                       DOLLARS SAVED   $0.00   ← subscription-billed, marginal cost ~$0
```

A gate that can't tell these apart reports savings that don't exist.

### Guardrails, each from a bug that shipped silently

| guardrail | what it prevents |
|---|---|
| `meg observe coverage` | a script calling the provider **around** the gate, on a model nobody evaluated |
| hard filters before price ranking | a cheaper model that can't meet the output contract being ranked at all |
| negative/absent price → infinite | router pseudo-models publishing `-1` and ranking first at *minus* $108M |
| free tiers excluded by default | a `$0` rank winning every comparison, on endpoints that rate-limit so hard the eval doesn't predict production |
| `validate()` before any spend | a grader that condemns every arm because it's measuring **itself** |
| cross-family judge panels | a judge inflating its own family's arm (measured at **+0.32** on a 1–5 scale) |
| unknown stakes ⇒ treated as high | a cheap model quietly making per-row decisions nobody audited |
| `COALESCE` merge on every upsert | a partial write blanking the columns the other half established — a call record arrives in two halves and neither carries the other's fields |

### Storage

SQLite by default (`~/.model-eval-gate/meg.db`, zero setup). Point `MEG_DB` or `--db` at a
`postgresql://` DSN to co-locate with an existing telemetry database — same schema, same
code path. The Postgres backend is covered by tests you can run yourself:

```bash
createdb meg_pg_test
MEG_TEST_PG="postgresql://$(whoami)@localhost:5432/meg_pg_test" python3 test/test_pipeline.py
dropdb meg_pg_test
```

The observe / cluster / propose / scaffold stages are **stdlib-only**; `psycopg2` is an
optional extra (`pip install 'model-eval-gate[postgres]'`) and `requests` is needed only by
the Python port's live-call path.

### Presets are hypotheses, not verdicts

The bundled modes were verified on someone else's data. Shipping them as verdicts would
violate the project's own founding rule, so a preset is **a starting hypothesis you confirm
on your own data**, not a permission you inherit. Your first regression run is your first
eval.

**Current state, stated plainly:** 2 of the 6 bundled modes ship a frozen regression spec.

| mode | regression spec |
|---|---|
| `filter-auto-reply` | ✅ `eval/regression/auto-reply.json` |
| `extract-accurate` | ✅ `eval/regression/extract-fields.json` |
| `extract-bulk` | ❌ — shares the extraction schema; needs its own throughput-shaped gold set |
| `digest-longcontext` | ❌ — needs an LLM-judge grader |
| `lint-code` | ❌ — needs an LLM-judge grader |
| `extract-multimodal` | ❌ — needs an image corpus |

A mode without a spec is a **hypothesis with no maintenance behind it**. Treat those four
as "someone else measured this once"; confirm before relying on them. Contributions of
specs (with fictionalised corpora) are the most useful PR you can send.

## Scope: a gate, not a sandbox

Be clear-eyed about the boundary. **model-eval-gate is a fail-closed gate for calls that go *through* it** — the CLI or the Python port. It is **not** a sandbox or a network-level policy boundary: an agent, service, or script that calls OpenRouter (or a provider) directly bypasses it entirely.

To make it a real control plane rather than a governed helper, **make it the only model-egress path** — e.g. run the calling code without provider API keys in its environment and expose only this wrapper, or put it behind an egress proxy that blocks direct provider domains. Within that boundary, the guarantees hold: unknown/retired modes are refused before any call, task metadata is checked against machine-readable constraints, and the policy file is validated on load (a malformed `routes.json` refuses everything rather than routing on garbage).

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
pip install -e .          # the `meg` pipeline (stdlib-only)
npm install               # the TS gate/CLI + regression harness
cp .env.example .env      # add your OPENROUTER_API_KEY
```

The `meg` pipeline needs Python 3.10+ and no third-party packages. The TS gate needs Node
18+. An [OpenRouter](https://openrouter.ai) key is required for live calls and the model
catalog; `meg observe ingest`, `meg workload list` and grader validation all work offline.

`meg observe sync` additionally needs `OPENROUTER_MANAGEMENT_KEY` — the daily-spend
endpoint rejects an inference key with a 403, and it only retains **30 days**, so schedule
it daily or you lose history permanently.

## Use

**As a CLI** (the enforcer):

```bash
npx tsx src/cli.ts help                          # the current allowlist, with verified dates
npx tsx src/cli.ts explain extract-bulk          # one mode's purpose, boundaries, evidence, pin, constraints
npx tsx src/cli.ts extract-bulk "<prompt>" --rows 5000   # runs — earned mode AND constraints satisfied
npx tsx src/cli.ts extract-bulk "<prompt>" --rows 3      # REFUSED — mode requires min_rows: 50
npx tsx src/cli.ts extract-multimodal "Read this roster" --image page.png
npx tsx src/cli.ts anything-else "..."           # refused — no earned mode
```

Task-eligibility flags (`--rows N`, `--stakes low|medium|high`, `--input text|image`, `--single-row`, `--human-reviewed`) are checked against each mode's `constraints` — so a mode can't be misused on a task it wasn't proven for, not just misnamed.

**From Python** (the same allowlist, no Node required):

```python
from src.router import text_call, vision_call, can_delegate
if can_delegate("extract-bulk", {"rows": 5000, "single_row_decision": False})["ok"]:
    text, usd = text_call("extract-bulk", "Extract fields:", big_text)
text, usd = vision_call("extract-multimodal", "Read the phone numbers.", "page.png")
# an off-allowlist mode raises ValueError — same refusal as the CLI
```

**Validate the policy offline** (no key, CI-friendly) and inspect a mode:

```bash
npx tsx src/check.ts        # validate routes.json + every regression spec (or: python src/router.py check)
npm run typecheck && npm test   # tsc + TS/Python parity tests — all offline
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

`eval/graders.ts` implements the [three grader kinds from Anthropic's evals guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents): **code** (deterministic — the default; field-agreement, normalized, enum, numeric-tolerance, array-set, must-not-contain, regex, json-subset), **model** (LLM-as-judge, opt-in), **human** (a recorded verdict frozen into the spec). A regression run separates model-quality drift from `provider_unavailable` / `parse_failure` / `grader_failure`, so a transient outage never looks like a regression.

## Layout

```
routes.json              the allowlist — eval verdicts as enforceable policy
src/schema.ts            zod validation of routes + specs (fail-closed on load)
src/cli.ts               the CLI enforcer (Node/tsx) — refuses everything off-allowlist
src/router.py            the Python port — same allowlist, stdlib + requests
src/preflight.ts         machine-checkable constraints → canDelegate(mode, taskMeta)
src/check.ts             offline policy validator (no API) — routes + every spec
eval/harness.ts          k-trial runner: pass@k / pass^k, outcome taxonomy, real cost
eval/graders.ts          the grader taxonomy + buildGrader factory
eval/regression.ts       re-run frozen specs, classify drift, exit non-zero
eval/regression/*.json   frozen specs + synthetic gold
test/                    offline TS+Python parity tests (shared golden fixtures)
GOVERNANCE.md            the policy: a mode requires a passing eval
docs/ADDING_A_MODE.md ·  docs/OBSERVATIONS.example.md
```

## Development

```bash
npm run typecheck    # tsc --noEmit
npm run check        # validate all policy files (offline)
npm test             # TS + Python parity tests (offline)
npm run format       # prettier
npm run ci           # all of the above — the same gate GitHub Actions runs
```

CI (`.github/workflows/ci.yml`) runs the offline gate on every push/PR. The live regression suite needs a key and runs on a schedule, not in CI.

## License

Apache-2.0 © 2026 Kai Karlstrom

## Data policy

This repository ships **no real data**. Every fixture, sample workload, example
observation and preset number is invented — fictional organisations, fictional people,
fictional model ids. Your telemetry stays in your own store (`~/.model-eval-gate/meg.db`
by default) and is never committed here.

If you contribute an eval writeup, generalise it first: the *shape* of the finding is the
useful part, and it travels without your customers' names attached.

---

<!-- portfolio-footer -->
## Where this fits

Part of a portfolio of **governed, AI-native GTM systems** — reference implementations and reusable patterns extracted from a private production stack. In that system this is the eval-backed gate that decides whether a call may be delegated to a cheaper model at all.

**Full portfolio map → [github.com/kkrlstrm](https://github.com/kkrlstrm)**

Works with:
- [cc-logger](https://github.com/kkrlstrm/cc-logger) — observes real delegation usage
- [codex-logger](https://github.com/kkrlstrm/codex-logger) — the Codex half of the same signal
