"""Turn raw telemetry into WORKLOAD CLASSES — the unit a routing decision is made on.

This is the inversion that makes the whole project different. Cost-first routers
start from a model catalog and ask "what's cheap?". Starting from observed work
means the first question is "what do I actually do, and which parts of it are
even eligible to move?" -- and eligibility is a property of the task, not the
price list.

A workload class is a group of calls that would succeed or fail together: same
task shape, same output contract, same stakes, same latency tolerance. If two
calls would need different evals to clear, they are different classes, however
similar their prompts look.

WHAT MAKES A CLASS ELIGIBLE TO MOVE (all measured, none assumed):

  volume            one call a month is not worth an eval, however expensive
  billing mix       subscription-covered work has ~$0 marginal cost, so moving it
                    saves nothing -- see `counterfactual`
  output contract   a schema-checkable output can be graded by code, which makes
                    a cheap, trustworthy eval possible; free text cannot
  stakes            per-row human-acted decisions are disqualified by default,
                    aggregate-feeding extraction is the classic safe case
  modality          vision/audio inputs hard-filter the candidate set

The stakes signal cannot be derived from telemetry alone -- nothing in a call
record says whether a human reads one row and acts on it. So it is inferred
conservatively (default: unknown => treated as high) and the user is asked to
confirm. Guessing "low" here is precisely how a cheap model ends up making
decisions nobody audited.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any

from ..store import Store


@dataclass
class WorkloadClass:
    id: str
    label: str
    source: str                       # "calls" | "harness"
    n: int = 0
    tokens_in_p50: int = 0
    tokens_in_p95: int = 0
    tokens_out_p50: int = 0
    tokens_out_p95: int = 0
    models_seen: list[str] = field(default_factory=list)
    schema_rate: float = 0.0          # fraction of calls using a response_format
    cost_usd: float = 0.0
    latency_p95_ms: int | None = None
    billing: str = "unknown"          # api | subscription | mixed
    stakes: str = "unknown"           # low | high | unknown  (unknown == treat as high)
    modality: str = "text"            # text | image | audio | file
    truncation_rate: float = 0.0
    evidence: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ derived
    @property
    def eligible(self) -> bool:
        """Whether this class may even be considered for delegation.

        Deliberately strict. A class excluded here costs a missed optimisation; a
        class wrongly included costs a silent quality regression on real work, and
        those are not symmetric."""
        return not self.blockers

    @property
    def blockers(self) -> list[str]:
        out = []
        if self.n < 30:
            out.append(f"too few observations ({self.n} < 30) to eval meaningfully")
        if self.billing == "subscription":
            out.append("subscription-billed: marginal cost is ~$0, so moving it "
                       "saves nothing (it buys rate-limit headroom, not dollars)")
        if self.stakes == "high":
            out.append("per-row decisions with consequences — stays with the orchestrator")
        if self.stakes == "unknown":
            out.append("stakes unconfirmed — treated as high until a human says otherwise")
        return out

    @property
    def gradeable_by_code(self) -> bool:
        """A schema on most calls means a deterministic grader is possible, which
        is what makes an eval cheap enough to actually run and re-run."""
        return self.schema_rate >= 0.8

    def to_dict(self) -> dict:
        d = asdict(self)
        d["eligible"] = self.eligible
        d["blockers"] = self.blockers
        d["gradeable_by_code"] = self.gradeable_by_code
        return d


def _pct(xs: list[int], p: float) -> int:
    if not xs:
        return 0
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))
    return int(xs[k])


def from_calls(store: Store, *, min_n: int = 1) -> list[WorkloadClass]:
    """Cluster recorded LLM calls by their workload tag.

    The tag is the honest grouping key: it is what the caller declared the work
    WAS, recorded at call time. Inferring classes from token statistics alone
    merges unrelated tasks that happen to have similar shapes -- a summariser and
    a classifier with the same input size are not the same workload.
    """
    rows = store.q("SELECT * FROM calls")
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r.get("workload") or "(untagged)"].append(r)

    out = []
    for tag, rs in sorted(groups.items()):
        if len(rs) < min_n:
            continue
        tin = [int(r["tokens_prompt"] or 0) for r in rs]
        tout = [int(r["tokens_completion"] or 0) for r in rs]
        lat = [int(r["latency_ms"]) for r in rs if r.get("latency_ms")]
        bills = {r.get("billing") for r in rs if r.get("billing")}
        schema_vals = [r.get("had_schema") for r in rs if r.get("had_schema") is not None]
        trunc = [r for r in rs if (r.get("finish_reason") or "") == "length"]
        out.append(WorkloadClass(
            id=f"calls:{tag}",
            label=tag,
            source="calls",
            n=len(rs),
            tokens_in_p50=_pct(tin, .5), tokens_in_p95=_pct(tin, .95),
            tokens_out_p50=_pct(tout, .5), tokens_out_p95=_pct(tout, .95),
            models_seen=sorted({r["model"] for r in rs if r.get("model")}),
            schema_rate=(sum(1 for v in schema_vals if v) / len(schema_vals)
                         if schema_vals else 0.0),
            cost_usd=round(sum(float(r["cost_usd"] or 0) for r in rs), 6),
            latency_p95_ms=_pct(lat, .95) if lat else None,
            billing=(bills.pop() if len(bills) == 1 else ("mixed" if bills else "unknown")),
            truncation_rate=round(len(trunc) / len(rs), 4),
            evidence={"modes": sorted({r["mode"] for r in rs if r.get("mode")})},
        ))
    return out


def from_harness(store: Store, *, min_n: int = 30) -> list[WorkloadClass]:
    """Cluster coding-agent tool calls into candidate workload classes.

    These are NOT LLM calls -- they are the work the agent did, which is where
    delegable tasks hide before anyone has thought to route them. A script run
    25,000 times is a workload whether or not a model is behind it today; a
    sub-agent prompt re-typed 200 times is a specialised worker waiting to be
    named. Both are invisible to any provider-side view.
    """
    rows = store.q("SELECT * FROM work_events WHERE kind = 'tool_call'")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        try:
            p = json.loads(r["payload"]) if isinstance(r.get("payload"), str) else (r.get("payload") or {})
        except (json.JSONDecodeError, TypeError):
            p = {}
        # Grouping key, most specific first: a named script is a task; a bare
        # argv0 is a habit; a repeated sub-agent prompt is an unnamed worker.
        if r.get("subagent") and p.get("prompt_sha"):
            key = ("subagent", r["subagent"], p["prompt_sha"][:8])
        elif p.get("script"):
            key = ("script", p["script"], "")
        else:
            key = ("tool", r.get("name") or "?", p.get("argv0") or "")
        groups[key].append({**r, "_p": p})

    out = []
    for (kind, a, b), rs in groups.items():
        if len(rs) < min_n:
            continue
        label = f"{a}" + (f" [{b}]" if b else "")
        bills = {r.get("billing") for r in rs if r.get("billing")}
        out.append(WorkloadClass(
            id=f"harness:{kind}:{a}:{b}".rstrip(":"),
            label=label,
            source="harness",
            n=len(rs),
            models_seen=sorted({r["agent_model"] for r in rs if r.get("agent_model")}),
            billing=(bills.pop() if len(bills) == 1 else ("mixed" if bills else "unknown")),
            evidence={
                "kind": kind,
                "harnesses": sorted({r["harness"] for r in rs}),
                "sessions": len({r["session_id"] for r in rs}),
                "prompt_len_p50": _pct([r["_p"].get("prompt_len", 0) for r in rs], .5) or None,
            },
        ))
    return sorted(out, key=lambda c: -c.n)


def counterfactual(cls: WorkloadClass, frontier_in: float, frontier_out: float,
                   candidate_in: float, candidate_out: float) -> dict:
    """What moving this class would actually save, stated honestly.

    Prices are $/1M tokens. The distinction that every cost-first tool gets wrong:
    if the work runs on a subscription today, its marginal cost is ~$0 and the
    'saving' from delegating it is **zero dollars** -- the number you would quote
    is the cost of LEAVING the subscription, not a saving. What you actually buy
    is rate-limit headroom and throughput, which are real benefits that should not
    be laundered into a dollar figure.
    """
    n, ti, to = cls.n, cls.tokens_in_p50, cls.tokens_out_p50
    cur = n * (ti * frontier_in + to * frontier_out) / 1e6
    new = n * (ti * candidate_in + to * candidate_out) / 1e6
    api_billed = cls.billing in ("api", "mixed")
    return {
        "observed_calls": n,
        "frontier_equivalent_usd": round(cur, 4),
        "candidate_usd": round(new, 4),
        "ratio": round(cur / new, 1) if new else None,
        "dollars_saved": round(cur - new, 4) if api_billed else 0.0,
        "billing": cls.billing,
        "note": ("real saving: this work is API-billed" if cls.billing == "api" else
                 "PARTIAL: mixed billing — only the API-billed share is a real saving"
                 if cls.billing == "mixed" else
                 "NO dollar saving: subscription-billed, marginal cost ~$0. "
                 "Moving it buys rate-limit headroom and throughput, not money."),
    }


def summarize(store: Store, *, min_harness_n: int = 30) -> dict:
    calls = from_calls(store)
    harness = from_harness(store, min_n=min_harness_n)
    return {
        "classes": [c.to_dict() for c in calls + harness],
        "eligible": [c.label for c in calls + harness if c.eligible],
        "coverage": store.coverage(),
    }
