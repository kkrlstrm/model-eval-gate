#!/usr/bin/env python3
"""
Python half of the offline test suite — the mirror of test/parity.ts. Asserts the
Python port agrees with the shared fixtures: same provider-pin wire mapping, same
constraint verdicts, same allowlist + refusals, plus fail-closed behavior and the
embedded-fallback consistency check. No network, no API key.

    python test/parity.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import router as r  # noqa: E402

fx = json.loads((ROOT / "test" / "fixtures.json").read_text())
routes = json.loads((ROOT / "routes.json").read_text())

failed = 0


def ok(name, cond, detail=""):
    global failed
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}{f' — {detail}' if detail else ''}")
        failed += 1


print("provider-pin wire mapping")
for c in fx["provider_pins"]:
    got = r._provider_http(c["pin"])
    ok(f"pin {json.dumps(c['pin'])}", got == c["http"], f"got {json.dumps(got)}")

print("constraint verdicts")
for c in fx["constraint_cases"]:
    got = r.check_constraints(c["constraints"], c["meta"])["ok"]
    ok(f"{json.dumps(c['constraints'])} vs {json.dumps(c['meta'])} -> {c['ok']}", got == c["ok"], f"got {got}")

print("allowlist + refusals")
for m in fx["refusals"]["must_exist"]:
    ok(f'mode "{m}" resolves', r._model_for(m) == routes["modes"][m]["model"])
for m in fx["refusals"]["unknown"]:
    try:
        r._model_for(m)
        ok(f'mode "{m}" refused', False, "did not raise")
    except ValueError:
        ok(f'mode "{m}" refused', True)

print("embedded-fallback consistency")
cons_ok, msg = r.routes_consistency()
ok("embedded matches canonical routes.json", cons_ok, msg)

print("\n✗ Python parity: %d failure(s)" % failed if failed else "\n✓ Python parity: all passed")
sys.exit(1 if failed else 0)
