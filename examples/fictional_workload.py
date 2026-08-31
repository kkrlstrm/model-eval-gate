#!/usr/bin/env python3
"""End-to-end walkthrough on FICTIONAL data.

Everything here is invented: Meridian Freight, Calder & Vance, the people, the
numbers, the model ids. No real organisation, customer, or telemetry appears in
this repository, and none should ever be committed to it.

The scenario is a made-up B2B operations team that:
  1. digests fetched supplier pages into structured fields   (high volume, schema'd)
  2. classifies inbound email as auto-reply or human         (binary, huge volume)
  3. writes the customer-facing summary of a shipment claim  (low volume, high stakes)

Run:  python3 examples/fictional_workload.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meg.store import Store
from meg.workload.cluster import WorkloadClass, counterfactual
from meg.workload.propose import propose
from meg.scaffold import SchemaGrader, RegexGrader, validate, score_panel
from meg.scaffold.spec import scaffold

# --------------------------------------------------------------------------- #
# A tiny fictional model catalogue, shaped like a real one.
# --------------------------------------------------------------------------- #
FAKE_CATALOG = [
    {"id": "atlas/atlas-pro-4", "name": "Atlas Pro 4",
     "pricing": {"prompt": "0.000003", "completion": "0.000015"},
     "context_length": 400_000,
     "architecture": {"input_modalities": ["text", "image"]},
     "supported_parameters": ["structured_outputs", "tools", "response_format"],
     "top_provider": {"context_length": 400_000, "max_completion_tokens": 64_000}},
    {"id": "bellwether/bw-flash", "name": "Bellwether Flash",
     "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
     "context_length": 1_000_000,
     "architecture": {"input_modalities": ["text"]},
     "supported_parameters": ["structured_outputs", "response_format"],
     "top_provider": {"context_length": 1_000_000, "max_completion_tokens": 32_000}},
    {"id": "cinder/cinder-mini", "name": "Cinder Mini",
     "pricing": {"prompt": "0.00000008", "completion": "0.0000003"},
     "context_length": 128_000,
     "architecture": {"input_modalities": ["text"]},
     "supported_parameters": ["response_format"],   # NB: no structured_outputs
     "top_provider": {"context_length": 128_000, "max_completion_tokens": 8_000}},
    {"id": "driftwood/dw-open:free", "name": "Driftwood Open (free)",
     "pricing": {"prompt": "0", "completion": "0"},
     "context_length": 64_000,
     "architecture": {"input_modalities": ["text"]},
     "supported_parameters": ["structured_outputs"],
     "top_provider": {"context_length": 64_000, "max_completion_tokens": 4_000}},
]

# --------------------------------------------------------------------------- #
# Three fictional workload classes.
# --------------------------------------------------------------------------- #
SUPPLIER_DIGEST = WorkloadClass(
    id="calls:supplier-page-digest", label="supplier-page-digest", source="calls",
    n=9_400, tokens_in_p50=11_000, tokens_in_p95=52_000,
    tokens_out_p50=780, tokens_out_p95=2_100,
    models_seen=["atlas/atlas-pro-4"], schema_rate=0.97,
    billing="api", stakes="low", cost_usd=402.11,
)
AUTOREPLY_FILTER = WorkloadClass(
    id="calls:inbound-autoreply-filter", label="inbound-autoreply-filter", source="calls",
    n=41_200, tokens_in_p50=900, tokens_in_p95=2_400,
    tokens_out_p50=8, tokens_out_p95=12,
    models_seen=["atlas/atlas-pro-4"], schema_rate=1.0,
    billing="api", stakes="low",
)
CLAIM_SUMMARY = WorkloadClass(
    id="calls:claim-summary-draft", label="claim-summary-draft", source="calls",
    n=310, tokens_in_p50=6_500, tokens_out_p50=1_400,
    models_seen=["atlas/atlas-pro-4"], schema_rate=0.0,
    billing="subscription", stakes="high",
)


def rule(t): print(f"\n{'─'*72}\n{t}\n{'─'*72}")


def main() -> int:
    rule("1. WHAT WORK EXISTS, AND WHAT MAY MOVE")
    for c in (SUPPLIER_DIGEST, AUTOREPLY_FILTER, CLAIM_SUMMARY):
        print(f"  {c.n:7,d}  {c.label:26s} billing={c.billing:13s} "
              f"eligible={'yes' if c.eligible else 'NO'}")
        for b in c.blockers:
            print(f"           └─ {b}")

    rule("2. CANDIDATES FOR supplier-page-digest (ranked on ITS token profile)")
    p = propose(SUPPLIER_DIGEST, FAKE_CATALOG, top_n=4)
    print(f"  capable {p['capable']}/{p['catalog_size']}   rejected: {p['rejected_by']}")
    for r in p["slate"]:
        tag = "  <- control" if r["is_control"] else ""
        print(f"  {r['id']:26s} in ${r['price_in_per_m']:>7.3f}/M  "
              f"out ${r['price_out_per_m']:>7.3f}/M  projected ${r['projected_usd']:>8.2f}{tag}")
    print("\n  Cinder Mini is cheapest per token but was ELIMINATED: no structured_outputs,")
    print("  and this class's outputs are schema-validated. Capability is a hard filter,")
    print("  not a tiebreaker. Driftwood is excluded as a free tier (it rate-limits, so")
    print("  an eval scored on it would not predict production).")

    rule("3. THE HONEST COUNTERFACTUAL")
    for c in (SUPPLIER_DIGEST, CLAIM_SUMMARY):
        cf = counterfactual(c, 3.0, 15.0, 0.10, 0.20)
        print(f"  {c.label}")
        print(f"    frontier-equivalent ${cf['frontier_equivalent_usd']:>9.2f}  "
              f"candidate ${cf['candidate_usd']:>8.2f}  ratio {cf['ratio']}x")
        print(f"    DOLLARS SAVED       ${cf['dollars_saved']:>9.2f}   <- {cf['note']}")

    rule("4. GRADER VALIDATION — BEFORE ANY MODEL SPEND")
    good = [({"supplier": "Meridian Freight", "status": "active"}, {}),
            ({"supplier": "Calder & Vance", "status": "lapsed"}, {})]
    bad = [({"supplier": "Meridian Freight"}, {}),
           ({"supplier": "X", "status": "banana"}, {}),
           ("not json at all", {})]
    ok = SchemaGrader(required=["supplier", "status"],
                      enums={"status": ["active", "lapsed"]})
    v = validate(ok, good, bad)
    print(f"  schema grader     valid={v['valid']}  fp={v['false_positives']} fn={v['false_negatives']}")

    # The cautionary one: a plausible rubric grader that is actually broken.
    sloppy = RegexGrader(r"freight|vance|status", must_match=False, field_key="supplier")
    v2 = validate(sloppy, good, bad)
    print(f"  keyword grader    valid={v2['valid']}  fn={v2['false_negatives']}")
    print(f"    {v2['verdict'][:68]}...")
    print("    Wrongly failed:", [o.get('supplier') for o, _ in v2['examples']['wrongly_failed']])
    print("    A grader like this returns uniformly low scores that read as a finding")
    print("    about the models. They are a finding about the grader.")

    rule("5. PANEL SANITY — SELF-PREFERENCE AND AGREEMENT")
    pv = score_panel({
        "judge-atlas": {"atlas-arm": [4.4, 4.3, 4.4], "bellwether-arm": [3.9, 4.0, 3.9]},
        "judge-bellwether": {"atlas-arm": [4.1, 4.2, 4.1], "bellwether-arm": [4.4, 4.5, 4.4]},
    })
    print("  means      :", {k: round(v, 2) for k, v in pv.mean.items()})
    print("  self-pref  :", pv.self_preference)
    print("  agreement  : within-1", pv.agreement_within_1)
    for w in pv.warnings:
        print("  WARN:", w)
    print("\n  Both judges favour their own family. That is why a single-judge panel")
    print("  cannot decide this, and why the delta is reported by default.")

    rule("6. THE EVAL SPEC A HUMAN THEN REVIEWS")
    spec = scaffold(
        SUPPLIER_DIGEST,
        prompt_source="ops/supplier_digest.py:DIGEST_PROMPT (imported verbatim)",
        task_summary="Extract supplier name, status and terms from a fetched page.",
        candidates=["bellwether/bw-flash"],
    )
    d = json.loads(spec.to_json())
    print(f"  id      : {d['id']}")
    print(f"  grader  : {d['grader']['kind']} — {d['grader']['grader']}")
    print(f"  control : {d['control']}   candidates: {d['candidates']}")
    print("  bar     :", list(d["strict_bar"]))
    print("  open questions:")
    for q in d["open_questions"]:
        print(f"    - {q}")

    # Store round-trip, so the example also proves the schema works.
    with tempfile.TemporaryDirectory() as td:
        s = Store(str(Path(td) / "demo.db"))
        s.upsert_calls([{
            "generation_id": "gen-fictional-1", "created_at": "2026-08-31T00:00:00+00:00",
            "workload": "supplier-page-digest", "mode": "extract-accurate",
            "model": "bellwether/bw-flash", "cost_usd": 0.0014,
            "tokens_prompt": 11000, "tokens_completion": 780,
            "billing": "api", "enriched": 1, "source": "recorder", "had_schema": 1}])
        s.upsert_spend([{"usage_date": "2026-08-31", "model": "bellwether/bw-flash",
                         "endpoint_id": "ep-1", "requests": 4, "cost_usd": 0.0060,
                         "synced_at": "2026-08-31T00:00:00+00:00"}])
        rule("7. COVERAGE — IS ANYTHING BYPASSING THE GATE?")
        cov = s.coverage()
        print(" ", json.dumps(cov))
        print(f"\n  The recorder saw 1 call; the provider billed {cov['provider_requests']}.")
        print("  The other 3 never went through the gate. On a real install that is the")
        print("  single most important number here — it is how you discover a script")
        print("  calling the provider directly on a model nobody evaluated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
