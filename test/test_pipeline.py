#!/usr/bin/env python3
"""Tests for the observe -> cluster -> propose -> validate pipeline.

All fixtures are FICTIONAL. No real telemetry, organisation, or customer data
appears in this repository.

The tests concentrate on the guardrails rather than the happy path, because every
one of them was written in response to a bug that shipped silently:

  - a Codex reader that captured 4% of tool calls because the record type is
    nested one level down
  - a cost ranker that put router pseudo-models first at minus $108M
  - free tiers winning every cost comparison at $0
  - enrichment nulling the workload tag it was supposed to be enriching
  - a grader that condemned all five arms of a bake-off because it was measuring
    itself

Run:  python3 test/test_pipeline.py     (or: pytest test/test_pipeline.py)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meg.store import Store
from meg.observe import harness
from meg.observe.recorder import _merge_preserving
from meg.workload.cluster import WorkloadClass, from_calls, from_harness, counterfactual
from meg.workload.propose import propose, filter_capable, _price, _is_free_tier
from meg.scaffold import SchemaGrader, RegexGrader, validate, score_panel
from meg.policy import decide, load_routes

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILS.append(name)
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {name}")


def truthy(name: str, cond) -> None:
    check(name, bool(cond), True)


def _store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "t.db"))


# --------------------------------------------------------------------------- #
def test_codex_nested_type(tmp: Path) -> None:
    """Codex nests the real record under payload.type.

    Reading only the envelope type yields turn rows and silently drops every tool
    call. This shipped, and cost ~96% of the Codex signal."""
    d = tmp / "codex"
    (d / "2026" / "08").mkdir(parents=True)
    f = d / "2026" / "08" / "sess-1.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [
        {"type": "response_item", "timestamp": "2026-08-31T00:00:00Z",
         "payload": {"type": "function_call", "name": "exec_command",
                     "arguments": json.dumps({"cmd": "python3 tools/ingest.py --all"})}},
        {"type": "response_item", "timestamp": "2026-08-31T00:00:01Z",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "input": "*** Begin Patch\n+hello\n"}},
        {"type": "response_item", "timestamp": "2026-08-31T00:00:02Z",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {"input_tokens": 120,
                                                   "output_tokens": 8}}}},
        # info=None happens on rate-limit-only records and must NOT become a row
        {"type": "response_item", "timestamp": "2026-08-31T00:00:03Z",
         "payload": {"type": "token_count", "info": None}},
        {"type": "turn_context", "timestamp": "2026-08-31T00:00:04Z",
         "payload": {"model": "vendor/model-x", "cwd": "/tmp"}},
    ]))
    evs = harness.read_codex(d)
    kinds = {}
    for e in evs:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    check("codex tool_calls found", kinds.get("tool_call"), 2)
    check("codex turn found", kinds.get("turn"), 1)
    check("codex token rows (null info dropped)", kinds.get("tokens"), 1)
    tc = [e for e in evs if e["kind"] == "tool_call"]
    scripts = {e["payload"].get("script") for e in tc}
    truthy("codex parses `cmd` into a script name", "ingest.py" in scripts)
    truthy("codex apply_patch keeps a shape", any(e["payload"] for e in tc))


def test_summarize_drops_content() -> None:
    """Telemetry must keep the SHAPE of a call, never its content.

    Users share these DBs when asking for routing help; prompts and arguments
    would carry customer data and credentials into that conversation."""
    p = harness._summarize_tool("Bash", {"command": "psql 'postgres://u:secret@h/db' -c 'select 1'"})
    blob = json.dumps(p)
    truthy("no secret in summary", "secret" not in blob)
    truthy("no full command in summary", "select 1" not in blob)
    check("argv0 kept", p.get("argv0"), "psql")
    p2 = harness._summarize_tool("Agent", {"prompt": "Find every supplier in Meridian Freight"})
    truthy("prompt content dropped", "Meridian" not in json.dumps(p2))
    truthy("prompt fingerprint kept", bool(p2.get("prompt_sha")))


def test_price_sanity() -> None:
    """Negative/absent prices must never rank first."""
    check("negative price -> inf", _price({"pricing": {"prompt": "-1"}}, "prompt"), float("inf"))
    check("missing price -> inf", _price({"pricing": {}}, "prompt"), float("inf"))
    check("free suffix detected", _is_free_tier({"id": "vendor/m:free"}), True)
    check("free pool detected", _is_free_tier({"id": "openrouter/free"}), True)
    check("normal model not free", _is_free_tier({"id": "vendor/m"}), False)


def test_hard_filters_before_price() -> None:
    """A cheaper model that cannot meet the contract is not an option at all."""
    cat = [
        {"id": "cheap/no-schema", "pricing": {"prompt": "0.00000001", "completion": "0.00000002"},
         "context_length": 200_000, "architecture": {"input_modalities": ["text"]},
         "supported_parameters": [], "top_provider": {"max_completion_tokens": 8000}},
        {"id": "fine/with-schema", "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
         "context_length": 200_000, "architecture": {"input_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 8000}},
        {"id": "vendor/x:free", "pricing": {"prompt": "0", "completion": "0"},
         "context_length": 200_000, "architecture": {"input_modalities": ["text"]},
         "supported_parameters": ["structured_outputs"],
         "top_provider": {"max_completion_tokens": 8000}},
    ]
    cls = WorkloadClass(id="c", label="c", source="calls", n=100, tokens_in_p50=1000,
                        tokens_out_p50=100, schema_rate=1.0, billing="api", stakes="low")
    out = propose(cls, cat, top_n=5)
    ids = [r["id"] for r in out["slate"]]
    truthy("schema-less cheap model excluded", "cheap/no-schema" not in ids)
    truthy("free tier excluded by default", "vendor/x:free" not in ids)
    truthy("capable model kept", "fine/with-schema" in ids)
    out2 = propose(cls, cat, top_n=5, include_free=True)
    truthy("free tier included on opt-in", "vendor/x:free" in [r["id"] for r in out2["slate"]])


def test_counterfactual_honesty() -> None:
    """Subscription work must report ZERO dollars saved, however big the ratio."""
    sub = WorkloadClass(id="s", label="s", source="calls", n=1000, tokens_in_p50=5000,
                        tokens_out_p50=500, billing="subscription", stakes="low")
    api = WorkloadClass(id="a", label="a", source="calls", n=1000, tokens_in_p50=5000,
                        tokens_out_p50=500, billing="api", stakes="low")
    cf_s = counterfactual(sub, 3.0, 15.0, 0.1, 0.2)
    cf_a = counterfactual(api, 3.0, 15.0, 0.1, 0.2)
    check("subscription saves $0", cf_s["dollars_saved"], 0.0)
    truthy("subscription ratio still reported", cf_s["ratio"] > 1)
    truthy("subscription note explains why", "rate-limit headroom" in cf_s["note"])
    truthy("api saving is real", cf_a["dollars_saved"] > 0)


def test_eligibility_defaults_to_no() -> None:
    """Unknown stakes must be treated as high, never as low."""
    c = WorkloadClass(id="c", label="c", source="calls", n=5000, billing="api",
                      stakes="unknown")
    check("unknown stakes blocks", c.eligible, False)
    truthy("blocker names the reason", any("stakes" in b for b in c.blockers))
    c2 = WorkloadClass(id="c", label="c", source="calls", n=5, billing="api", stakes="low")
    check("tiny sample blocks", c2.eligible, False)


def test_grader_validation_rejects_the_real_broken_grader() -> None:
    """The regression that motivates the whole validate() stage.

    A rubric grader flagged any label sharing a token with a role name, so a
    correct output containing an ordinary domain word was scored wrong. Every arm
    looked bad; the grader was the problem."""
    good = [({"label": "technology refresh deadline"}, {}),
            ({"label": "end of fiscal year funding push"}, {})]
    bad = [({"label": "generic pitch to technology directors"}, {})]
    broken = RegexGrader(r"technology|director", must_match=False, field_key="label")
    v = validate(broken, good, bad)
    check("broken grader rejected", v["valid"], False)
    truthy("names the false negatives", v["false_negatives"] >= 1)

    ok = SchemaGrader(required=["label", "kind"], enums={"kind": ["a", "b"]})
    v2 = validate(ok, [({"label": "x", "kind": "a"}, {})],
                  [({"label": "x"}, {}), ({"label": "x", "kind": "zz"}, {})])
    check("sound grader accepted", v2["valid"], True)


def test_panel_flags_self_preference_and_single_judge() -> None:
    one = score_panel({"judge-atlas": {"atlas-arm": [5, 5], "other-arm": [3, 3]}})
    check("single judge unusable", one.usable, False)
    truthy("single-judge warning", any("single judge" in w for w in one.warnings))

    two = score_panel({
        "judge-atlas": {"atlas-arm": [4.5, 4.5], "bellwether-arm": [3.5, 3.5]},
        "judge-bellwether": {"atlas-arm": [4.0, 4.0], "bellwether-arm": [4.1, 4.1]}})
    truthy("self-preference measured", two.self_preference.get("judge-atlas", 0) > 0.25)
    truthy("self-preference warned", any("self-preference" in w for w in two.warnings))


def test_enrichment_preserves_local_fields() -> None:
    """The provider has never heard of `mode`, `workload`, `billing`, `had_schema`.

    A naive upsert of its record nulls them, destroying the attribution the
    recorder exists to create."""
    s = _store()
    s.upsert_calls([{"generation_id": "gen-1", "created_at": "2026-08-31T00:00:00+00:00",
                     "workload": "supplier-digest", "mode": "extract-accurate",
                     "model": "vendor/a", "billing": "api", "had_schema": 1,
                     "enriched": 0, "source": "recorder"}])
    _merge_preserving(s, "gen-1", {
        "generation_id": "gen-1", "created_at": "2026-08-31T00:00:00+00:00",
        "workload": None, "mode": None, "model": "vendor/a", "billing": None,
        "had_schema": None, "cost_usd": 0.004, "enriched": 1, "source": "generation"})
    r = s.q("SELECT * FROM calls WHERE generation_id = 'gen-1'")[0]
    check("workload preserved", r["workload"], "supplier-digest")
    check("mode preserved", r["mode"], "extract-accurate")
    check("billing preserved", r["billing"], "api")
    check("cost updated", round(float(r["cost_usd"]), 4), 0.004)
    check("marked enriched", int(r["enriched"]), 1)


def test_coverage_detects_bypass() -> None:
    s = _store()
    s.upsert_calls([{"generation_id": "g1", "created_at": "x", "model": "m",
                     "workload": "w", "cost_usd": 0.001}])
    s.upsert_spend([{"usage_date": "2026-08-31", "model": "m", "endpoint_id": "e",
                     "requests": 10, "cost_usd": 0.01, "synced_at": "x"}])
    c = s.coverage()
    check("untracked counted", c["untracked_requests"], 9)
    check("coverage pct", c["coverage_pct"], 10.0)


def test_calls_cluster_by_tag() -> None:
    s = _store()
    for i in range(3):
        s.upsert_calls([{"generation_id": f"g{i}", "created_at": "x", "model": "m",
                         "workload": "supplier-digest", "tokens_prompt": 1000 + i,
                         "tokens_completion": 100, "billing": "api", "had_schema": 1,
                         "cost_usd": 0.001}])
    s.upsert_calls([{"generation_id": "gx", "created_at": "x", "model": "m",
                     "workload": None, "tokens_prompt": 5, "billing": "api"}])
    cs = {c.label: c for c in from_calls(s)}
    check("tagged class found", cs["supplier-digest"].n, 3)
    check("schema rate", cs["supplier-digest"].schema_rate, 1.0)
    truthy("untagged bucketed separately", "(untagged)" in cs)


def test_policy_parity_with_ts() -> None:
    """Python `decide()` must agree with the TS `decide()` in the OpenClaw plugin.

    Both read test/policy_cases.json, so a case cannot be added to one side and
    forgotten on the other. An agent governed by the OpenClaw plugin and a script
    governed by the Python library must never reach opposite conclusions about
    the same task -- that is how one policy quietly becomes two."""
    root = Path(__file__).resolve().parent.parent
    cases = json.loads((root / "test" / "policy_cases.json").read_text())
    routes = load_routes(root / "routes.json")
    for c in cases:
        d = decide(c["mode"], c["meta"], routes=routes,
                   require_full_metadata=c.get("strict", False))
        check(f"policy: {c['name'][:52]}", d.allowed, c["expect"]["allowed"])
        want = c["expect"].get("reason_contains")
        if want:
            truthy(f"  reason mentions {want!r}", want.lower() in d.reason.lower())


def test_policy_fails_closed() -> None:
    """No readable policy => nothing is earned. Never a permissive default."""
    d = decide("extract-bulk", {"rows": 9999},
               routes={"modes": {}, "retired": {}, "_error": "unreadable"})
    check("unreadable policy refuses", d.allowed, False)
    truthy("says why", "policy unavailable" in d.reason)


def test_postgres_backend() -> None:
    """The Postgres path, exercised for real — or skipped loudly.

    `Store` advertises `postgresql://` support, and an advertised backend that has
    only ever run on SQLite is a claim, not a feature. This covers the two things
    that actually differ between the dialects: the `?` -> `%s` placeholder
    rewrite, and whether `ON CONFLICT ... DO UPDATE SET x = excluded.x` behaves
    the same.

    Opt in with a DSN pointing at a SCRATCH database:
        MEG_TEST_PG=postgresql://user@localhost:5432/meg_pg_test python3 test/test_pipeline.py
    It creates and writes tables, so never point it at anything you care about.
    """
    dsn = os.environ.get("MEG_TEST_PG")
    if not dsn:
        print("  SKIP postgres (set MEG_TEST_PG=postgresql://... to run it)")
        return
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        print("  SKIP postgres (pip install 'model-eval-gate[postgres]')")
        return

    s = Store(dsn)
    check("pg dialect detected", s.pg, True)
    s.upsert_calls([{"generation_id": "pg-1", "created_at": "2026-08-31T00:00:00+00:00",
                     "workload": "demo", "mode": "extract-accurate", "model": "vendor/a",
                     "cost_usd": 0.001, "tokens_prompt": 100, "tokens_completion": 10,
                     "billing": "api", "had_schema": 1, "enriched": 0,
                     "source": "recorder", "meta": {"k": "v"}}])
    s.upsert_calls([{"generation_id": "pg-1", "created_at": "2026-08-31T00:00:00+00:00",
                     "workload": "demo", "model": "vendor/a", "cost_usd": 0.009,
                     "enriched": 1, "source": "generation"}])
    row = s.q("SELECT cost_usd, enriched FROM calls WHERE generation_id = ?", ["pg-1"])[0]
    check("pg on-conflict updates", round(float(row["cost_usd"]), 4), 0.009)
    check("pg placeholder rewrite works", int(row["enriched"]), 1)

    s.upsert_spend([{"usage_date": "2026-08-31", "model": "vendor/a", "endpoint_id": "e1",
                     "requests": 5, "cost_usd": 0.01, "synced_at": "2026-08-31T00:00:00+00:00"}])
    s.upsert_work([{"event_id": "pg-e1", "harness": "codex", "session_id": "s1",
                    "started_at": "2026-08-31", "kind": "tool_call", "name": "exec",
                    "payload": {"argv0": "ls"}}])
    check("pg coverage computes", s.coverage()["untracked_requests"], 4)

    _merge_preserving(s, "pg-1", {
        "generation_id": "pg-1", "created_at": "2026-08-31T00:00:00+00:00",
        "workload": None, "mode": None, "model": "vendor/a", "billing": None,
        "had_schema": None, "cost_usd": 0.5, "enriched": 1, "source": "generation"})
    r2 = s.q("SELECT workload, billing FROM calls WHERE generation_id = ?", ["pg-1"])[0]
    check("pg preserves local fields", (r2["workload"], r2["billing"]), ("demo", "api"))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print("codex reader");            test_codex_nested_type(tmp)
    print("content redaction");           test_summarize_drops_content()
    print("price sanity");                test_price_sanity()
    print("hard filters before price");   test_hard_filters_before_price()
    print("counterfactual honesty");      test_counterfactual_honesty()
    print("eligibility defaults");        test_eligibility_defaults_to_no()
    print("grader validation");           test_grader_validation_rejects_the_real_broken_grader()
    print("panel sanity");                test_panel_flags_self_preference_and_single_judge()
    print("enrichment preservation");     test_enrichment_preserves_local_fields()
    print("coverage / bypass");           test_coverage_detects_bypass()
    print("call clustering");             test_calls_cluster_by_tag()
    print("policy parity (shared cases)"); test_policy_parity_with_ts()
    print("policy fails closed");         test_policy_fails_closed()
    print("postgres backend");            test_postgres_backend()
    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failing check(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
