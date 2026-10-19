#!/usr/bin/env python3
"""The route-eval contract, checked from RunTune's side.

test/route_eval.ts writes test/route_eval_golden.json from eval/route_eval.ts. This
runs RunTune's own validator (runtune/verify/route_eval.py `check`) over it, so a
field renamed in either repo fails here. RunTune is found on the import path, or at
$RUNTUNE_REPO (default ../runtune next to this checkout). Without it the test says so
and exits 0: the TypeScript half still guards the document's shape.

    python3 test/contract_runtune.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "test" / "route_eval_golden.json"

try:
    from runtune.verify import route_eval
except ImportError:
    repo = Path(os.environ.get("RUNTUNE_REPO", ROOT.parent / "runtune"))
    sys.path.insert(0, str(repo))
    try:
        from runtune.verify import route_eval
    except ImportError:
        print(f"  - RunTune not found (set RUNTUNE_REPO); route-eval contract not checked from its side")
        sys.exit(0)

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
failed = 0


def ok(name, cond, detail=""):
    global failed
    print(f"  {'✓' if cond else '✗'} {name}{f' — {detail}' if not cond and detail else ''}")
    failed += not cond


print("route-eval contract (RunTune's validator)")
req = {"mode": "extract-fields", "candidate": "cand/a", "incumbent": "inc/b"}
problems = route_eval.check(req, str(GOLDEN), now=NOW)
ok("the golden result is usable for its own change", problems == [], "; ".join(problems))
other = route_eval.check({**req, "candidate": "cand/z"}, str(GOLDEN), now=NOW)
ok("and is refused for a different candidate", bool(other))
print("\n✗ contract: %d failure(s)" % failed if failed else "\n✓ contract: all passed")
sys.exit(1 if failed else 0)
