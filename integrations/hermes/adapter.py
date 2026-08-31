"""model-eval-gate — Hermes adapter (ADVISORY, by necessity not by choice).

Hermes exposes a `pre_llm_call` plugin hook, but its agent loop currently treats
a hook's result as optional user-message context — so a plugin **cannot cleanly
override the model** from it. Doing so today means monkey-patching
`AIAgent._run_agent_loop()` or similar internals, which is brittle across
releases. There is an open upstream request for request-scoped model/provider
override: NousResearch/hermes-agent#23739.

So this adapter DECIDES AND RECORDS, and does not enforce.

That limitation is stated loudly rather than engineered around. Monkey-patching
the loop would produce a gate that appears to work and silently stops working on
the next Hermes release — and a gate that quietly stops gating is worse than no
gate, because the absence of refusals reads as compliance. The honest version is
useful today (workload inventory, eligibility verdicts, the counterfactual, a
log of every decision that WOULD have been enforced) and becomes enforcing the
moment the upstream hook can return an override, with no change to policy.

    from integrations.hermes.adapter import HermesGate
    gate = HermesGate(workload_map={"enrich-nightly": {"mode": "extract-bulk",
                                                       "meta": {"rows": 5000}}})
    # in a pre_llm_call plugin:
    advice = gate.advise(task_name="enrich-nightly")
    # advice.enforced is always False on Hermes today; advice.decision is real.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from meg.policy import Decision, decide, load_routes  # noqa: E402

log = logging.getLogger("model-eval-gate.hermes")

UPSTREAM_ISSUE = "https://github.com/NousResearch/hermes-agent/issues/23739"


@dataclass
class Advice:
    decision: Decision
    enforced: bool
    note: str

    @property
    def model(self) -> str | None:
        """The model that WOULD be used if Hermes could accept an override."""
        return self.decision.model if self.decision.allowed else None


class HermesGate:
    """Advisory delegation gate for Hermes plugins."""

    def __init__(self, workload_map: dict[str, dict] | None = None,
                 routes_path: str | Path | None = None,
                 require_full_metadata: bool = True,
                 audit_path: str | Path | None = None):
        self.map = workload_map or {}
        self.routes = load_routes(routes_path)
        # Defaults to True here even though the Python library defaults to False:
        # a Hermes plugin runs unattended, and an unattended runtime has nobody
        # reading the warning that a constraint went unverified.
        self.require_full_metadata = require_full_metadata
        self.audit_path = Path(audit_path) if audit_path else None
        if self.routes.get("_error"):
            log.warning("[model-eval-gate] %s — every delegation will be refused.",
                        self.routes["_error"])

    def advise(self, task_name: str | None = None, *, mode: str | None = None,
               meta: dict | None = None) -> Advice:
        """Decide whether `task_name` (or an explicit mode) may be delegated."""
        decl = None
        if mode:
            decl = {"mode": mode, "meta": meta or {}}
        elif task_name and task_name in self.map:
            decl = self.map[task_name]

        if not decl:
            d = Decision(False, mode or task_name or "(untagged)",
                         reason="no declared workload — an untagged task has "
                                "earned nothing, so it stays on the frontier model")
        else:
            d = decide(decl["mode"], decl.get("meta") or {}, routes=self.routes,
                       require_full_metadata=self.require_full_metadata)

        adv = Advice(
            decision=d,
            enforced=False,
            note=("ADVISORY ONLY on Hermes: pre_llm_call cannot return a model "
                  f"override today (see {UPSTREAM_ISSUE}). The decision below is "
                  "real and recorded; the call will still run on Hermes' "
                  "configured model."),
        )
        self._audit(task_name, adv)
        return adv

    def _audit(self, task: str | None, adv: Advice) -> None:
        verdict = f"WOULD-ALLOW -> {adv.decision.model}" if adv.decision.allowed else "REFUSE"
        log.info("[model-eval-gate] %s task=%s mode=%s %s",
                 verdict, task, adv.decision.mode, adv.decision.reason)
        if adv.decision.allowed and adv.decision.stale_days and adv.decision.stale_days > 120:
            log.warning("[model-eval-gate] mode=%s last verified %dd ago — an old "
                        "verdict is a hypothesis, not a fact.",
                        adv.decision.mode, adv.decision.stale_days)
        if self.audit_path:
            rec = {"task": task, "enforced": adv.enforced, **adv.decision.to_dict()}
            with self.audit_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")


def pre_llm_call(gate: HermesGate, task_name: str | None = None):
    """Shape of a Hermes `pre_llm_call` plugin entry point.

    Returns None so the turn is untouched — which is all this can do today, and
    all it SHOULD do until the override path exists. Wire the returned advice
    into your own telemetry if you want the would-have-been decisions visible.
    """
    def _hook(*_args, **_kwargs):
        gate.advise(task_name=task_name)
        return None
    return _hook
