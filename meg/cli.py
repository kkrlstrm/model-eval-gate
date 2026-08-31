"""meg — the model-eval-gate CLI.

    meg observe ingest            read Claude Code + Codex sessions into the store
    meg observe sync              pull the provider's daily spend rollup
    meg observe enrich            backfill per-call detail from generation records
    meg observe coverage          is anything reaching the provider around the gate?

    meg workload list             what work you actually do, and what may move
    meg workload propose <id>     candidate models for one class, ranked on YOUR tokens

    meg eval scaffold <id>        generate a reviewable eval spec
    meg eval validate-grader      prove a grader discriminates before you spend

    meg gate check                validate routes.json (see also: src/cli.ts)

The staging is deliberate. Each command answers one question and stops, because
the decisions in between -- is this class really low-stakes? is that grader
actually sound? -- are human calls the pipeline must not make for you.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def _key(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    env = ROOT / ".env"
    if env.exists():
        import re
        d = dict(re.findall(r"^\s*(\w+)\s*=\s*[\"']?(.*?)[\"']?\s*$", env.read_text(), re.M))
        return d.get(name)
    return None


# --------------------------------------------------------------------- observe
def cmd_observe(a) -> int:
    from .observe import harness, provider
    store = Store(a.db)

    if a.action == "ingest":
        stats = harness.ingest(store, claude_code=not a.no_claude_code,
                               codex=not a.no_codex)
        for k, v in stats.items():
            print(f"{k:14s} {v:,} events")
        by = store.q("SELECT harness, kind, count(*) n FROM work_events "
                     "GROUP BY harness, kind ORDER BY n DESC")
        print()
        for r in by:
            print(f"  {r['harness']:14s} {r['kind']:12s} {r['n']:,}")
        return 0

    if a.action == "sync":
        mk = _key("OPENROUTER_MANAGEMENT_KEY")
        if not mk:
            print("OPENROUTER_MANAGEMENT_KEY not set. The daily rollup requires a "
                  "MANAGEMENT key; an inference key returns 403.", file=sys.stderr)
            return 2
        r = provider.sync_activity(mk, store)
        print(f"{r['rows']} rows · {r['window'][0]} → {r['window'][1]} · ${r['window_usd']}")
        print(f"ledger: {r['ledger_rows']} rows spanning {r['ledger_span'][0]} → {r['ledger_span'][1]}")
        if r["history_gap"]:
            print("\nWARNING: the provider still offers data older than your ledger's "
                  "first row. History was lost before collection started, and the "
                  "window is 30 days — schedule this daily.", file=sys.stderr)
        return 0

    if a.action == "enrich":
        from .observe.recorder import enrich
        k = _key("OPENROUTER_API_KEY")
        if not k:
            print("OPENROUTER_API_KEY not set (per-generation records are readable by "
                  "the INFERENCE key, not the management key).", file=sys.stderr)
            return 2
        print(json.dumps(enrich(store, k, limit=a.limit), indent=1))
        return 0

    if a.action == "coverage":
        c = store.coverage()
        print(json.dumps(c, indent=1))
        if c["untracked_requests"]:
            print(f"\n{c['untracked_requests']:,} provider requests did NOT come through "
                  "the recorder.\nSomething is calling the provider around the gate — "
                  "that is the bypass this tool exists to detect.", file=sys.stderr)
            return 1
        return 0
    return 2


# -------------------------------------------------------------------- workload
def cmd_workload(a) -> int:
    from .workload import from_calls, from_harness, propose, fetch_catalog
    store = Store(a.db)
    classes = from_calls(store) + from_harness(store, min_n=a.min_n)

    if a.action == "list":
        if not classes:
            print("no workload classes yet — run `meg observe ingest` first.")
            return 0
        print(f"{'n':>9s}  {'src':8s} {'billing':13s} {'elig':5s}  class")
        for c in sorted(classes, key=lambda c: -c.n):
            print(f"{c.n:9,d}  {c.source:8s} {c.billing:13s} "
                  f"{'yes' if c.eligible else 'no':5s}  {c.label[:52]}")
        blocked = [c for c in classes if not c.eligible]
        if blocked:
            print(f"\n{len(blocked)} class(es) not eligible. Most common reasons:")
            seen: dict[str, int] = {}
            for c in blocked:
                for b in c.blockers:
                    seen[b.split(":")[0].split("—")[0].strip()] = seen.get(
                        b.split(":")[0].split("—")[0].strip(), 0) + 1
            for reason, n in sorted(seen.items(), key=lambda kv: -kv[1]):
                print(f"  {n:4d}  {reason}")
        return 0

    if a.action == "propose":
        match = [c for c in classes if c.label == a.workload or c.id == a.workload]
        if not match:
            print(f"no class matching {a.workload!r}. Try `meg workload list`.", file=sys.stderr)
            return 2
        cat = fetch_catalog(_key("OPENROUTER_API_KEY"))
        out = propose(match[0], cat, top_n=a.top, include_free=a.include_free)
        print(json.dumps(out, indent=1))
        if not out["eligible"]:
            print("\nNOTE: this class is not eligible to move. The slate above is "
                  "informational only.", file=sys.stderr)
        return 0
    return 2


# ------------------------------------------------------------------------ eval
def cmd_eval(a) -> int:
    from .workload import from_calls, from_harness
    from .scaffold.spec import scaffold
    store = Store(a.db)

    if a.action == "scaffold":
        classes = from_calls(store) + from_harness(store, min_n=1)
        match = [c for c in classes if c.label == a.workload or c.id == a.workload]
        if not match:
            print(f"no class matching {a.workload!r}", file=sys.stderr)
            return 2
        spec = scaffold(match[0],
                        prompt_source=a.prompt_source or "<TODO: import from production>",
                        task_summary=a.summary or "<TODO: one line>",
                        candidates=(a.candidates or "").split(",") if a.candidates else [])
        print(spec.to_json())
        return 0

    if a.action == "validate-grader":
        print("Grader validation is a library call — it needs your known-good and\n"
              "known-bad examples, which only you have:\n\n"
              "    from meg.scaffold import SchemaGrader, validate\n"
              "    g = SchemaGrader(required=['label'], enums={'kind': ['a','b']})\n"
              "    print(validate(g, known_good, known_bad))\n\n"
              "It must pass BEFORE you spend anything on candidates. A grader that\n"
              "cannot separate good from bad produces confident numbers that describe\n"
              "the grader, not the models.")
        return 0
    return 2


# ------------------------------------------------------------------------ gate
def cmd_gate(a) -> int:
    routes = ROOT / "routes.json"
    if not routes.exists():
        print("routes.json missing — the gate fails closed: every mode is refused.",
              file=sys.stderr)
        return 1
    data = json.loads(routes.read_text())
    modes, retired = data.get("modes", {}), data.get("retired", {})
    print(f"{len(modes)} allowed mode(s), {len(retired)} retired\n")
    for name, m in modes.items():
        print(f"  {name:22s} {m.get('model','?')}")
        if not m.get("do_not_use_when"):
            print("      ^ MISSING do_not_use_when — the negative constraint is what "
                  "prevents misuse; a mode without one is not gated.")
    if retired:
        print("\nretired:")
        for name, r in retired.items():
            print(f"  {name:22s} {r.get('retired_date','?')}  {r.get('reason','')[:60]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # `--db` is accepted on BOTH sides of the subcommand. Argparse only honours a
    # top-level option before the subcommand, so `meg workload list --db x` would
    # otherwise be a usage error — and that is the order everyone actually types.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="SQLite path or postgresql:// URL")

    p = argparse.ArgumentParser(prog="meg", description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("observe", parents=[common]); o.add_argument("action",
        choices=["ingest", "sync", "enrich", "coverage"])
    o.add_argument("--no-claude-code", action="store_true")
    o.add_argument("--no-codex", action="store_true")
    o.add_argument("--limit", type=int, default=500)
    o.set_defaults(fn=cmd_observe)

    w = sub.add_parser("workload", parents=[common]); w.add_argument("action", choices=["list", "propose"])
    w.add_argument("workload", nargs="?")
    w.add_argument("--min-n", type=int, default=30)
    w.add_argument("--top", type=int, default=6)
    w.add_argument("--include-free", action="store_true",
                   help="include free tiers (excluded by default: they rate-limit, so "
                        "an eval scored on one does not predict production)")
    w.set_defaults(fn=cmd_workload)

    e = sub.add_parser("eval", parents=[common]); e.add_argument("action",
        choices=["scaffold", "validate-grader"])
    e.add_argument("workload", nargs="?")
    e.add_argument("--prompt-source"); e.add_argument("--summary")
    e.add_argument("--candidates")
    e.set_defaults(fn=cmd_eval)

    g = sub.add_parser("gate", parents=[common]); g.add_argument("action", choices=["check"])
    g.set_defaults(fn=cmd_gate)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
