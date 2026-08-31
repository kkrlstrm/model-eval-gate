"""Candidate proposal — which models could plausibly do THIS class of work.

Two stages, in this order, and the order matters:

  1. HARD FILTERS. Capability requirements are binary. A class whose outputs are
     schema-validated needs `structured_outputs`; a class reading page images
     needs an image input modality; a class digesting 300k-token documents needs
     the context. A model that fails any of these is not a cheap option, it is
     not an option, and ranking it by price would be nonsense.

  2. RANK BY COST AT THE CLASS'S OWN TOKEN PROFILE. Not list price. This is not a
     refinement -- it changes the answer. In a real bake-off a candidate with a
     *higher* list output price beat the control on total spend, because the
     control read far more input per row. Price-per-token tells you nothing until
     you multiply it by how many tokens this particular workload actually uses.

What this stage explicitly does NOT do is pick a winner. It produces a slate to
eval. The whole premise is that capability claims and price tables do not predict
quality on your data; only a measurement does.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from .cluster import WorkloadClass

CATALOG_URL = "https://openrouter.ai/api/v1/models/user"
CACHE = Path.home() / ".model-eval-gate" / "catalog.json"
CACHE_TTL_S = 24 * 3600


def fetch_catalog(api_key: str | None = None, *, refresh: bool = False) -> list[dict]:
    """The provider's live model list. Cached a day — it changes, but not hourly."""
    if not refresh and CACHE.exists() and (time.time() - CACHE.stat().st_mtime) < CACHE_TTL_S:
        return json.loads(CACHE.read_text())["data"]
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        if CACHE.exists():
            return json.loads(CACHE.read_text())["data"]
        raise RuntimeError("no OPENROUTER_API_KEY and no cached catalog")
    req = urllib.request.Request(CATALOG_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.load(r)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(body))
    return body["data"]


def _price(m: dict, field: str) -> float:
    """$/1M tokens. The catalog quotes $/token as a string; a missing, unparseable
    or NEGATIVE price is treated as infinite rather than free, so a model with no
    usable published price can never win a cost ranking by accident.

    The negative case is not hypothetical: the provider's own auto-router
    pseudo-models publish `-1` as a "priced dynamically" sentinel, and taking that
    literally ranked them first at minus one hundred million dollars."""
    try:
        v = float((m.get("pricing") or {}).get(field)) * 1e6
    except (TypeError, ValueError):
        return float("inf")
    return v if v >= 0 else float("inf")


def _is_free_tier(m: dict) -> bool:
    """Free variants are excluded from ranking by default.

    Not snobbery — a measurement problem. Free endpoints rate-limit aggressively
    and swap capacity underneath you, so an eval scored on one does not predict
    production, and a $0 rank makes them win every cost comparison automatically.
    The reference policy this project was extracted from retired an entire `free`
    mode for exactly this. Opt in with `include_free=True` if you want them.
    """
    mid = m.get("id", "")
    # Two spellings in the wild: a `:free` variant suffix, and a standalone
    # `openrouter/free` pool. Matching only the suffix let the pool through and it
    # ranked first at $0 — the exact failure this guard exists to prevent.
    return mid.endswith(":free") or mid.startswith("openrouter/free")


def _is_meta_router(m: dict) -> bool:
    """Auto-router pseudo-models pick a real model at call time, so they cannot be
    the subject of an eval: the thing you measured is not the thing that runs."""
    return m.get("id", "").startswith("openrouter/auto")


def requirements(cls: WorkloadClass) -> dict:
    """Derive hard capability requirements from what the class was observed doing."""
    ctx = max(cls.tokens_in_p95 + cls.tokens_out_p95, cls.tokens_in_p50 * 2, 4096)
    return {
        "structured_outputs": cls.gradeable_by_code,
        "modality": cls.modality,
        "min_context": int(ctx * 1.25),   # headroom: p95 is not the maximum
        "min_completion": max(int(cls.tokens_out_p95 * 1.5), 256),
    }


def filter_capable(catalog: list[dict], req: dict, *,
                   include_free: bool = False) -> tuple[list[dict], dict]:
    """Apply hard filters. Returns (survivors, why-each-rule-eliminated-how-many).

    The rejection tally is returned because 'no candidates' is a common and
    confusing outcome, and the useful answer is *which* requirement is doing the
    eliminating -- usually context length or a modality, rarely price."""
    rejected = {"structured_outputs": 0, "modality": 0, "context": 0,
                "completion": 0, "free_tier": 0, "meta_router": 0, "no_price": 0}
    out = []
    for m in catalog:
        arch = m.get("architecture") or {}
        params = m.get("supported_parameters") or []
        top = m.get("top_provider") or {}
        if _is_meta_router(m):
            rejected["meta_router"] += 1
            continue
        if _is_free_tier(m) and not include_free:
            rejected["free_tier"] += 1
            continue
        if _price(m, "prompt") == float("inf") or _price(m, "completion") == float("inf"):
            rejected["no_price"] += 1
            continue
        if req["structured_outputs"] and "structured_outputs" not in params:
            rejected["structured_outputs"] += 1
            continue
        if req["modality"] != "text" and req["modality"] not in (arch.get("input_modalities") or []):
            rejected["modality"] += 1
            continue
        ctx = m.get("context_length") or top.get("context_length") or 0
        if ctx and ctx < req["min_context"]:
            rejected["context"] += 1
            continue
        maxc = top.get("max_completion_tokens")
        if maxc and maxc < req["min_completion"]:
            rejected["completion"] += 1
            continue
        out.append(m)
    return out, rejected


def cost_for(cls: WorkloadClass, m: dict) -> float:
    """Projected spend for this class's observed volume on this model."""
    pin, pout = _price(m, "prompt"), _price(m, "completion")
    if pin == float("inf") or pout == float("inf"):
        return float("inf")
    return cls.n * (cls.tokens_in_p50 * pin + cls.tokens_out_p50 * pout) / 1e6


def propose(cls: WorkloadClass, catalog: list[dict], *, top_n: int = 6,
            control: str | None = None, include_free: bool = False) -> dict:
    """Produce a candidate slate for one workload class.

    Always includes the incumbent/control arm even if it is expensive: a bake-off
    without the thing you currently use measures nothing you can act on.
    """
    req = requirements(cls)
    capable, rejected = filter_capable(catalog, req, include_free=include_free)
    ranked = sorted(capable, key=lambda m: cost_for(cls, m))

    control = control or (cls.models_seen[0] if cls.models_seen else None)
    slate, seen = [], set()
    for m in ranked:
        if len(slate) >= top_n:
            break
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        slate.append(m)
    if control and control not in seen:
        ctrl = next((m for m in catalog if m["id"] == control), None)
        if ctrl:
            slate.append(ctrl)

    def row(m: dict) -> dict:
        return {
            "id": m["id"],
            "name": m.get("name"),
            "price_in_per_m": round(_price(m, "prompt"), 4),
            "price_out_per_m": round(_price(m, "completion"), 4),
            "context": m.get("context_length"),
            "projected_usd": round(cost_for(cls, m), 4),
            "is_control": m["id"] == control,
        }

    return {
        "workload": cls.label,
        "eligible": cls.eligible,
        "blockers": cls.blockers,
        "requirements": req,
        "catalog_size": len(catalog),
        "capable": len(capable),
        "rejected_by": rejected,
        "slate": [row(m) for m in slate],
        "note": ("Ranked by projected spend at THIS class's measured token profile "
                 f"({cls.tokens_in_p50} in / {cls.tokens_out_p50} out, n={cls.n}), "
                 "not by list price. A slate is not a verdict — it is what to eval."),
    }
