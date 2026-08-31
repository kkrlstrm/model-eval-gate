"""Scaffold an eval from a workload class.

The output is a spec a human reviews, not a job that auto-runs. Two reasons, both
learned the hard way:

  1. The stakes field cannot be derived from telemetry. Nothing in a call record
     says whether a person reads one row and acts on it. An auto-run pipeline
     would have to guess, and guessing "low" is how a cheap model quietly ends up
     making decisions nobody audited.

  2. The grader is the part most likely to be wrong (see graders.validate), and a
     wrong grader produces confident numbers. A human glancing at 20 sample rows
     and the proposed grader catches this in a minute; a pipeline never will.

THE PROMPT IS IMPORTED, NEVER REWRITTEN. An eval that paraphrases the production
prompt measures a task you do not run. Both reference evals that produced
actionable results imported the live prompt verbatim from the production module;
that is the single highest-leverage detail in this file.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any

from ..workload.cluster import WorkloadClass


@dataclass
class EvalSpec:
    id: str
    workload: str
    created: str
    task_summary: str
    prompt_source: str
    n_samples: int
    grader: dict
    control: str | None
    candidates: list[str]
    stakes: str
    billing: str
    counterfactual: dict = field(default_factory=dict)
    strict_bar: dict = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    sample_ids: list[str] = field(default_factory=list)

    def to_json(self, indent: int = 1) -> str:
        return json.dumps(asdict(self), indent=indent)


def propose_grader(cls: WorkloadClass) -> dict:
    """Pick a grader kind from what the workload's outputs actually look like.

    Preference order is code -> panel -> human, because that is the order of
    trustworthiness and cost, and because a deterministic grader can be validated
    exhaustively while a panel can only be sampled.
    """
    if cls.gradeable_by_code:
        return {
            "kind": "code",
            "grader": "schema",
            "why": (f"{cls.schema_rate:.0%} of observed calls used a response_format, "
                    "so outputs have a checkable contract — grade with code, not a model."),
            "todo": ["list required keys", "list any enum-valued fields",
                     "supply a gold `expected` per sample if one exists"],
        }
    if cls.tokens_out_p50 and cls.tokens_out_p50 < 400:
        return {
            "kind": "panel",
            "grader": "cross-family panel",
            "why": ("short free-text output with no schema — no deterministic contract "
                    "to check, so use judges."),
            "todo": ["pick >= 2 judges from DIFFERENT model families",
                     "strip arm identity and shuffle position per item",
                     "report self-preference delta and judge agreement by default"],
        }
    return {
        "kind": "human",
        "grader": "human review",
        "why": ("long free-text output with no contract. A panel here is measuring "
                "taste, and taste disagreements are not a routing signal."),
        "todo": ["sample 20-30 items for human scoring", "define the rubric first"],
    }


def strict_bar(cls: WorkloadClass) -> dict:
    """The bar a candidate must clear. Tightens as the consequences rise.

    'Measurable quality parity, not good enough with caveats' — if a
    recommendation needs an 'if you scaffold the prompt with...' clause, it does
    not qualify.
    """
    base = {
        "quality": "no worse than control on the primary metric",
        "consistency": "pass^k over k>=3 trials — an unstable win is not a win",
        "no_new_failure_mode": "candidate must not introduce an error class the "
                               "control does not have (e.g. fabrication, silent truncation)",
    }
    if cls.stakes != "low":
        base["human_review"] = ("stakes are not confirmed low — a candidate may only "
                                "be adopted behind human review of its output")
    if cls.truncation_rate > 0.01:
        base["truncation"] = (f"control truncates on {cls.truncation_rate:.1%} of calls; "
                              "candidate must not be worse")
    return base


def scaffold(cls: WorkloadClass, *, prompt_source: str, task_summary: str,
             candidates: list[str], samples: list[dict] | None = None,
             n_samples: int = 200, seed: int = 20260831) -> EvalSpec:
    """Build a reviewable eval spec for one workload class."""
    rng = random.Random(seed)
    picked: list[dict] = []
    if samples:
        # Stratify head vs tail: the most frequent inputs carry the blast radius,
        # but a set made only of them hides the long-tail failures that matter.
        ranked = sorted(samples, key=lambda s: -(s.get("frequency") or 0))
        head = ranked[: n_samples // 2]
        tail_pool = ranked[n_samples // 2:]
        tail = rng.sample(tail_pool, min(n_samples - len(head), len(tail_pool)))
        picked = head + tail

    q = [
        "Confirm the stakes: does a human read a single output row and act on it? "
        "If yes, this class stays with the orchestrator regardless of eval results.",
        "Is the prompt imported verbatim from production, or paraphrased? "
        "A paraphrase measures a task you do not run.",
    ]
    if cls.billing != "api":
        q.append(f"Billing is '{cls.billing}': confirm what a win here actually buys. "
                 "If this work runs on a subscription, the dollar saving is zero — "
                 "the gain is rate-limit headroom and throughput.")
    if not picked:
        q.append("No samples attached — supply real inputs. A synthetic corpus "
                 "cannot clear this project's bar.")

    return EvalSpec(
        id=f"eval-{cls.label.replace('/', '-').replace(' ', '-')}-{date.today().isoformat()}",
        workload=cls.label,
        created=date.today().isoformat(),
        task_summary=task_summary,
        prompt_source=prompt_source,
        n_samples=len(picked) or n_samples,
        grader=propose_grader(cls),
        control=(cls.models_seen[0] if cls.models_seen else None),
        candidates=candidates,
        stakes=cls.stakes,
        billing=cls.billing,
        strict_bar=strict_bar(cls),
        open_questions=q,
        sample_ids=[str(s.get("id")) for s in picked][:n_samples],
    )
