# model-eval-gate v2 — architecture

**Status:** design, 2026-08-31. Supersedes the v1 README's scope (gate + regression only).
**Repo:** `github.com/kkrlstrm/model-eval-gate`

---

## The thesis

Every existing tool in this space is a **cost-first router**: here is a catalog of
models, here are their prices, send the work to a cheap one, maybe apply a quality
heuristic. `universal-agent-config` and strixgate both work this way.

The catalog is the wrong starting point. This project inverts the pipeline:

> **observed work → workload classes → candidate models → eval on your data →
> a gate with a refusal → a regression that keeps it honest**

The model catalog enters at step 3, and only as a filter. What decides everything
upstream is *what you actually do* — which you can read, because your coding agent
already writes it down.

### Why refusal is the product

A cheap model works beautifully on 80% of a task shape, gets generalised into "use
this for extraction," and then handles the 20% where it fails **silently**. Nobody
notices until a human acts on a wrong row. Cost-first routers accelerate this;
they have no vocabulary for "this task may not be delegated."

The asset here is not the allowlist. It is the **retired** block — the modes that
were tried, measured, and refused, each with a date and a reason. No competitor
has an equivalent.

### The counterfactual nobody computes

Most "savings" reported by routing tools are fictional, because the work was
already paid for.

If a call runs inside a Claude Code / Codex session on a subscription, the frontier
model's marginal cost is **$0**. Routing it to OpenRouter saves nothing; it buys
rate-limit headroom and throughput, which are real but are not dollars. The saving
is real only for calls that would otherwise be **API-billed** — unattended jobs,
containers, CI, anything with no subscription behind it.

A gate that cannot tell these apart will confidently report savings that do not
exist. So the counterfactual is computed **per call site**, from telemetry, and is
a first-class field on every eval verdict.

---

## Seven properties that make the approach work

These are not implementation details; they are the spec.

1. **Modes are named for the use case, not the model.** `extract-bulk`,
   `filter-auto-reply`, `digest-longcontext` — never `qwen`, never `cheap`. A mode
   named after a model is a vibes lookup table that rots on the next release; a mode
   named after a task shape forces you to describe what you are doing and makes
   misuse obvious. This is why an allowlist can survive four model generations as a
   config edit rather than a refactor.
2. **A mode exists only because a recorded eval on real data cleared a strict bar.**
   Not synthetic data. Not "good enough with caveats." If the recommendation needs
   an "if you scaffold the prompt with…" clause, it does not qualify.
3. **`do_not_use_when` is as load-bearing as `use_when`.** The negative constraint
   is the safety mechanism. It is what stops a model cleared for binary
   classification from being quietly promoted to multi-class.
4. **One source of truth, with drift guards.** `routes.json` is read live by every
   runtime. A consistency assertion fails the build if an embedded fallback drifts.
   Provider pinning closes eval→prod endpoint drift, so production runs on the
   endpoint the eval was scored on.
5. **Capability evals graduate into regression evals.** pass@k (shots on goal) and
   pass^k (consistency) turn a one-time verdict into a maintained guarantee.
6. **Retirement is explicit.** A failed re-eval moves a mode to `retired` with a
   dated reason and a refusal message. Never a silent delete.
7. **The artifact is verified, not just the model.** Re-running an eval against a
   live endpoint proves the endpoint is fine. It does not prove the data already in
   your database is fine.

---

## Six failure modes the design must defend against

Each of these was observed in a live system, not hypothesised.

| # | Failure | Requirement it creates |
|---|---|---|
| 1 | **The chokepoint gets bypassed.** Two production scripts called the provider with a raw HTTP POST, using a model that was never evaluated. | Enforce at the **HTTP boundary**, not at a CLI. A convention is not enforcement. |
| 2 | **Comments drift from code.** One call site named three different models across its docstring, an inline comment, and its constant. | The routing decision must be **derived and introspectable** (`model_for(mode)` resolved live), never documented. |
| 3 | **Modes go dead.** A flagship mode received **1 request in 30 days** while its traffic silently migrated to an unevaluated sibling. | Feed usage back onto the allowlist. Flag modes nobody calls, and models called by nobody's mode. |
| 4 | **Graders are wrong before models are.** A rubric grader condemned all five arms of a bake-off; it was measuring itself, not them. | **Grader validation is a mandatory pipeline stage.** A grader that cannot separate known-good from known-bad is rejected before any model spend. |
| 5 | **Judges prefer their own family.** In a blind panel, the judge from family A scored family A's arm **+0.32** above the others; a second judge scored it −0.04. | Panels must be **cross-family**, and the self-preference delta must be reported by default, not on request. |
| 6 | **Stored artifacts drift from live behaviour.** Two independent evals found the data in production scored *worse* than a fresh run of the same model on the same prompt. | Regressions must re-verify the **artifact**, not only the endpoint. |

---

## Telemetry: what is actually obtainable

### From the coding agent (the workload signal)

Two harnesses, two mechanisms, one schema — matching the split already proven by
`cc-logger` and `codex-logger`:

- **Claude Code** — lifecycle **HTTP hooks** (`PreToolUse` / `PostToolUse` /
  session events), plus the local transcript JSONL as a backfill source.
- **Codex** — hooks only fire for shell commands, so instead **tail the append-only
  rollout JSONL** under `~/.codex/sessions/**`, which already records every session,
  turn, tool call, model, and per-turn token count. Hook-independent by design.

This is what tells you *what work you do*: task shapes, tool sequences, repeated
sub-agent prompts, where time and failures go.

### From the provider (the cost and routing signal)

Probed against OpenRouter, 2026-08-31:

| endpoint | grain | retention | key |
|---|---|---|---|
| `/credits` | account lifetime total | forever | any |
| `/key` | this key: usage + daily/weekly/monthly | rolling | any |
| `/keys` | **per-key** usage, limits, disabled | rolling | management |
| `/activity` | day × model × endpoint: requests, tokens, spend | **30 days** | management |
| `/generation?id=` | **one call, 45 fields** | — | **the key that made the call** |
| `/models/user` | full catalog: pricing, context, modalities, `supported_parameters` | live | any |

**The hard limit that shapes the whole design: there is no endpoint that lists your
generations.** You can only look one up *by id*, and the id exists only in the
response body. Per-call telemetry is therefore reachable **only if you capture the
id at call time**; everything before you start doing that is unreachable forever.
The daily rollup is also 30-day retained, so a missed day is permanently lost.

Two operational quirks worth encoding: a generation takes **~5–10 seconds** to
become queryable (the first fetch 404s), and `/generation` is readable by the
**inference** key while `/activity` requires the **management** key — so a complete
collector needs both.

**Attribution round-trips, and this is the unlock.** Sending `"user":
"skill:find-dials/run-42"` in the request body comes back on the generation record
as `external_user`; an `HTTP-Referer` header comes back as `origin`. So the provider
will carry a workload tag on every call and hand it back later. That turns a
model-shaped ledger ("what did deepseek cost this month") into a workload-shaped one
("what did find-dials cost this month") — which is the only shape a routing decision
can be made from.

Per-generation fields worth having: `total_cost`, `cache_discount`,
`native_tokens_{prompt,completion,reasoning,cached}`, `latency` vs
`generation_time`, `finish_reason` + `native_finish_reason` (truncation detection),
`provider_name` and the served `endpoint_id` (the provider-pin drift check).

**What you do not get:** prompt/response content, tool calls, or conversation
structure. The provider is a flight recorder for spend and routing; the harness
adapters are the flight recorder for reasoning. Both are required — neither
substitutes for the other.

---

## Pipeline

```
observe ──▶ cluster ──▶ propose ──▶ scaffold ──▶ validate ──▶ run ──▶ gate ──▶ maintain
   │           │           │           │            │          │        │         │
harness     workload    candidate     eval       grader     bake-off  routes  regression
adapters    classes     slate from    spec +     sanity     vs        .json   + staleness
+ call                  /models/user  samples    check      control           + dead-mode
recorder                                                                       feedback
```

**observe** — pluggable collectors. The call recorder is also the enforcement
boundary, so instrumentation and gating are the same object: you cannot make an
un-tagged call through the sanctioned path.

**cluster** — the differentiator, and the hard part. Turn raw calls into *workload
classes*: task shape, input/output token distributions, whether a `response_format`
is present, downstream use (aggregate vs per-row), stakes, latency tolerance,
volume, and the subscription counterfactual. Nobody else has this because nobody
else starts from observed work.

**propose** — join each class against the live catalog. Hard filters first (needs
structured outputs? 1M context? vision? audio?), then rank by cost **at that class's
measured token profile**, not at list price. This matters: in a real bake-off a
model with a *higher* list output price beat the control on total cost, because the
control read more input per row.

**scaffold** — pull N real samples from telemetry, choose a grader by output type
(code grader when schema-checkable → cross-family model panel when generative →
human when neither), and **import the production prompt verbatim** so the eval
measures the real task rather than a rewrite of it.

**validate** — run the grader against known-good and known-bad before spending a
cent on candidates. See failure mode 4.

**run / gate / maintain** — bake-off with the incumbent as control; strict bar;
`use_when` / `do_not_use_when`; frozen regression spec; staleness warnings;
dead-mode feedback.

---

## Shipping

**v1 — one click.** The recorder, the enforcement boundary, the activity ledger,
and a set of pre-verified modes as a **preset**.

> **The trap to avoid:** the presets are verified on *someone else's* data.
> Shipping them as verdicts violates property 2 and turns this into the thing it
> criticises. Every preset ships **with its frozen regression spec**, framed as *a
> starting hypothesis you confirm on your own data in one command*. That framing is
> also the funnel into v2: the user's first regression run is their first eval.

**v2 — the loop.** observe → cluster → propose → scaffold for whatever work the
user actually does, with new-mode recommendations that a human approves.

---

## Positioning

> Cost-first routers tell you what is cheap.
> This tells you what is **safe to move** — and what isn't.

---

## Data policy

The repo ships **no real data**. All fixtures, sample workloads, eval corpora and
`OBSERVATIONS` entries are fictional, using invented organisations and invented
people. Real telemetry stays in the user's own store and is never committed. The
example preset's numbers are illustrative and are marked as such.
