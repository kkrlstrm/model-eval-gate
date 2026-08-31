# Integrations — governed delegation for agent runtimes

> The agent runtime decides **what work to do**.
> model-eval-gate decides **whether that work may be delegated to a smaller model**.
> Agents can plan freely. They cannot downgrade freely.

Every integration is a thin translation layer over one decision function
([`meg/policy.py:decide()`](../meg/policy.py)). Adapters hold no policy of their own — that
is deliberate, because two adapters with their own interpretations is exactly how a gate
starts meaning different things in different runtimes.

## The adapter contract

An adapter needs four things from its host runtime. Only the first two are strictly
required; without the third it is advisory, and without the fourth it cannot detect its own
bypass.

| # | Capability | Used for |
|---|---|---|
| 1 | A pre-call hook, before the model is resolved | ask the gate |
| 2 | Some way to tag a unit of work with a **mode + metadata** | know *what* is being asked |
| 3 | The ability to **override** the model from that hook | enforce the answer |
| 4 | Post-call usage/telemetry | reconcile authorised vs billed → detect bypass |

Capability 4 is what the OpenClaw adapter has only partially: it *observes* usage but does
not yet persist it, so reconciliation is not wired end to end.

Given those, an adapter is roughly twenty lines:

```python
from meg.policy import decide

d = decide(mode, meta, require_full_metadata=True)
if d.allowed:
    use_model(d.model, provider=d.provider)   # earned permission
else:
    pass                                       # leave the frontier model alone
audit(d)                                       # always record the decision
```

**The refusal path is a no-op, not an error.** Refusing means "do this work the way you were
already going to" — so adopting the gate can slow a workload down or cost more, but it
cannot break one.

## Status by runtime

| runtime | pre-call hook | can override the model? | status |
|---|---|---|---|
| **OpenClaw** | `before_model_resolve` | ✅ returns `{providerOverride, modelOverride}` | **enforcing** |
| **Hermes** | `pre_llm_call` | ⚠️ not cleanly — see below | **advisory only** |
| anything else | — | — | write ~20 lines against the contract above |

### OpenClaw — enforcing

[`integrations/openclaw/`](openclaw/). OpenClaw's `before_model_resolve` runs before the
session's model is resolved and may return `{ providerOverride, modelOverride }`; returning
nothing means no override. That maps onto this policy exactly, because a refusal *is*
"return nothing." (It replaced the deprecated `before_agent_start` in OpenClaw 2026.4.21.)

```ts
import { register } from "model-eval-gate/integrations/openclaw";

export default (api) =>
  register(api, {
    requireFullMetadata: true, // unattended: an unevidenced constraint refuses
    workloadMap: {
      // tag work without touching the agent's own code
      "nightly-enrichment": { mode: "extract-bulk", meta: { rows: 5000, single_row_decision: false } },
      "inbox-triage": { mode: "filter-auto-reply", meta: { stakes: "low" } },
    },
  });
```

The plugin only ever moves a turn **down** to a smaller model that has earned a permission
for that workload. It never upgrades, never chooses between frontier models, and never
substitutes on price.

**Scope of the current adapter, stated precisely** — it returns `modelOverride` for approved
work, records its policy decisions, and emits usage diagnostics from `llm_output`. Three
things it does *not* do yet, each a runtime integration step rather than a policy change:

- it does **not persist** OpenClaw calls into the telemetry store, so provider-ledger
  reconciliation (`meg observe coverage`) does not yet see them;
- a `nudge` **surfaces a warning to the runtime/operator**; it is not injected into the
  prompt. Doing that needs a `before_prompt_build` integration, at which point the note
  becomes context the model can self-correct on;
- OpenClaw's `providerOverride` takes a provider **name**, so a multi-field provider pin
  (order / allowFallbacks / quantizations) is reduced to its first entry. The pin exists to
  make production run on the endpoint the eval was scored on, and a name alone does not
  guarantee that — pin fidelity is weaker here than through the Python path.

### Hermes — advisory only, and why

Hermes exposes `pre_llm_call`, but its core loop currently treats a hook's result as
optional user-message context — so a plugin **cannot cleanly override the model** from it.
Doing so today means monkey-patching agent-loop internals, which is brittle across releases.
There is an open upstream request for exactly this capability
([NousResearch/hermes-agent#23739](https://github.com/NousResearch/hermes-agent/issues/23739)).

So the Hermes adapter **decides and records but does not enforce.** That is stated plainly
rather than worked around: a gate that silently fails to gate is worse than no gate, because
the absence of refusals reads as compliance. It still earns its keep — you get the workload
inventory, the eligibility verdicts, the honest counterfactual, and a log of every decision
that *would* have been enforced — and it becomes enforcing the moment the upstream hook can
return an override.

## Where this sits

- **Agent runtime** (OpenClaw, Hermes, your own loop) — skills, tools, sessions, sub-agents.
  Decides *what to do*.
- **Gateway** (OpenRouter, LiteLLM) — provider selection, fallback, cost, throughput.
  Executes *the call*.
- **model-eval-gate** — proof-backed permission to delegate *this particular work*.
  Decides *whether the call may be downgraded at all*.

These compose. This is not a replacement for a gateway or an eval platform: they execute
calls and measure quality; this governs the decision to delegate, and keeps that decision
maintained after it is made.

## Claim discipline

This is a **reference implementation**. It governs calls that pass through its wrapper or a
registered hook. A tool that shells out to a provider directly, or a sub-process holding its
own API key, bypasses it — which is why `meg observe coverage` reconciles recorded calls
against provider-side request counts and treats a persistent gap as a policy violation
rather than a telemetry nuisance.
