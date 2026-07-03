#!/usr/bin/env python3
"""
model-eval-gate (Python port) — the same eval-gated allowlist, callable from Python.

The CLI enforcer (src/cli.ts) is the interactive front door; this module is the
library equivalent so the SAME policy travels with a copy that doesn't ship
Node/tsx. It maps each approved mode to exactly one verified model, refuses
anything else, enforces per-mode constraints (canDelegate), and calls OpenRouter
directly over HTTPS.

FAIL-CLOSED: routes.json is the source of truth. If it's missing or invalid, all
modes are refused by default. A minimal embedded fallback exists ONLY as an
explicit opt-in (MODEL_EVAL_GATE_ALLOW_EMBEDDED=1) for a distributed copy shipped
without routes.json; even then, `routes_consistency()` asserts it hasn't drifted.

NOTE ON SCOPE: this is a fail-closed gate for calls that go THROUGH it. It is not a
sandbox or network policy boundary — code that calls OpenRouter directly bypasses
it. To make it a real control plane, make this the only model-egress path.

Public API:
    text_call(mode, instruction, stdin_text, timeout=180) -> (text|None, usd)
    vision_call(mode, prompt, image_path, timeout=180)    -> (text|None, usd)
    can_delegate(mode, meta) -> {"ok": bool, "violations": [...], "unchecked": [...]}

    python src/router.py <mode> "<prompt>" [--rows N --stakes low --input text --single-row --human-reviewed]
    python src/router.py check | explain <mode> | help
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

# NOTE: `requests` is imported lazily inside _http_post — the allowlist, validation,
# preflight, check, and explain all work with zero third-party deps (stdlib only).

ROOT = Path(__file__).resolve().parent.parent
ROUTES_PATH = ROOT / "routes.json"
SPEC_DIR = ROOT / "eval" / "regression"
_OR_HTTP = "https://openrouter.ai/api/v1/chat/completions"
_HTTP_TEXT_CAP = 400_000
_ALLOW_EMBEDDED = os.environ.get("MODEL_EVAL_GATE_ALLOW_EMBEDDED") == "1"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAKES_RANK = {"low": 0, "medium": 1, "high": 2}
_RESERVED_GENERIC = {"chat", "cheap", "cheapest", "free", "online", "general",
                     "default", "auto", "fast", "model", "llm", "any", "fallback"}
_GRADER_KINDS = {"fieldAgreement", "normalizedFieldAgreement", "enumMatch",
                 "numericWithinTolerance", "arraySetMatch", "mustNotContain",
                 "regexMatch", "jsonSubset", "human", "llmJudge"}

# Minimal embedded fallback — mode -> {model, provider}. Only reachable with the
# explicit opt-in above. Keep in sync with routes.json; routes_consistency() flags drift.
_EMBEDDED_ROUTES: dict[str, dict] = {
    "extract-bulk":       {"model": "qwen/qwen3-235b-a22b-2507",     "provider": None},
    "filter-auto-reply":  {"model": "google/gemini-2.5-flash-lite",  "provider": None},
    "lint-code":          {"model": "mistralai/codestral-2508",      "provider": None},
    "digest-longcontext": {"model": "deepseek/deepseek-v4-flash",    "provider": None},
    "extract-accurate":   {"model": "deepseek/deepseek-v4-flash",    "provider": None},
    "extract-multimodal": {"model": "minimax/minimax-m3",            "provider": None},
}


def _load_full() -> dict | None:
    """The full validated-shape routes object, or None if missing/unparseable."""
    try:
        raw = json.loads(ROUTES_PATH.read_text())
        if raw.get("modes"):
            return raw
    except Exception:  # noqa: BLE001
        pass
    return None


_FULL = _load_full()


def _routes_map() -> dict[str, dict]:
    """mode -> {model, provider}. routes.json when present; else fail-closed (empty)
    unless the embedded fallback is explicitly allowed."""
    if _FULL is not None:
        return {k: {"model": v["model"], "provider": v.get("provider")}
                for k, v in _FULL["modes"].items()}
    if _ALLOW_EMBEDDED:
        print("[model-eval-gate] routes.json missing — using embedded fallback "
              "(MODEL_EVAL_GATE_ALLOW_EMBEDDED=1)", file=sys.stderr)
        return _EMBEDDED_ROUTES
    print("[model-eval-gate] routes.json missing/invalid and embedded fallback not "
          "allowed — refusing ALL modes (fail-closed). Set MODEL_EVAL_GATE_ALLOW_EMBEDDED=1 "
          "to override.", file=sys.stderr)
    return {}


_ROUTES = _routes_map()


def env(name: str) -> str:
    v = os.environ.get(name)
    if v:
        return v
    envfile = ROOT / ".env"
    if envfile.is_file():
        for line in envfile.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _model_for(mode: str) -> str:
    try:
        return _ROUTES[mode]["model"]
    except KeyError:
        raise ValueError(
            f"model-eval-gate: mode {mode!r} is not on the delegation allowlist "
            f"({', '.join(sorted(_ROUTES)) or 'NONE — routes.json missing, fail-closed'}). "
            f"New modes require a passing eval per GOVERNANCE.md."
        )


def _provider_for(mode: str) -> dict | None:
    return (_ROUTES.get(mode) or {}).get("provider")


def _mode_def(mode: str) -> dict | None:
    if _FULL and mode in _FULL.get("modes", {}):
        return _FULL["modes"][mode]
    return None


def _provider_http(pin: dict | None) -> dict | None:
    """routes.json pin (SDK camelCase) -> OpenRouter chat/completions body (snake_case)."""
    if not pin:
        return None
    out: dict = {}
    for k in ("order", "only", "quantizations"):
        if pin.get(k) is not None:
            out[k] = pin[k]
    if pin.get("allowFallbacks") is not None:
        out["allow_fallbacks"] = pin["allowFallbacks"]
    return out or None


def check_constraints(c: dict | None, meta: dict | None = None) -> dict:
    """Pure constraint checker (mirror of src/preflight.ts). meta keys: rows,
    stakes, input_type, single_row_decision, human_review."""
    meta = meta or {}
    violations: list[str] = []
    unchecked: list[str] = []
    if not c:
        return {"ok": True, "violations": violations, "unchecked": unchecked}
    if c.get("min_rows") is not None:
        if meta.get("rows") is None:
            unchecked.append(f"min_rows={c['min_rows']} (no row count supplied)")
        elif meta["rows"] < c["min_rows"]:
            violations.append(f"min_rows: task has {meta['rows']} rows, needs >= {c['min_rows']}")
    if c.get("forbid_single_row_decision"):
        if meta.get("single_row_decision") is None:
            unchecked.append("forbid_single_row_decision (not stated)")
        elif meta["single_row_decision"]:
            violations.append("forbid_single_row_decision: this output drives a single-row decision")
    if c.get("requires_human_review"):
        if meta.get("human_review") is None:
            unchecked.append("requires_human_review (not stated)")
        elif not meta["human_review"]:
            violations.append("requires_human_review: no human review in the loop")
    if c.get("allowed_input_types"):
        it = meta.get("input_type")
        if it is None:
            unchecked.append(f"allowed_input_types={'|'.join(c['allowed_input_types'])} (no input type supplied)")
        elif it not in c["allowed_input_types"]:
            violations.append(f"allowed_input_types: {it!r} not in {'|'.join(c['allowed_input_types'])}")
    if c.get("max_stakes"):
        st = meta.get("stakes")
        if st is None:
            unchecked.append(f"max_stakes={c['max_stakes']} (no stakes supplied)")
        elif _STAKES_RANK.get(st, 0) > _STAKES_RANK[c["max_stakes"]]:
            violations.append(f"max_stakes: task is {st!r}, mode allows <= {c['max_stakes']!r}")
    return {"ok": not violations, "violations": violations, "unchecked": unchecked}


def can_delegate(mode: str, meta: dict | None = None) -> dict:
    """Resolve a mode's constraints and check task metadata against them."""
    md = _mode_def(mode)
    return check_constraints((md or {}).get("constraints"), meta)


def routes_consistency() -> tuple[bool, str]:
    """Assert the embedded fallback matches canonical routes.json when both exist."""
    if not ROUTES_PATH.is_file():
        return True, "no canonical routes.json present — embedded fallback is authoritative"
    try:
        canon = {k: v["model"] for k, v in (json.loads(ROUTES_PATH.read_text()).get("modes") or {}).items()}
    except Exception as e:  # noqa: BLE001
        return False, f"canonical routes.json unreadable: {e.__class__.__name__}"
    emb = {k: v["model"] for k, v in _EMBEDDED_ROUTES.items()}
    if canon == emb:
        return True, f"embedded fallback matches canonical ({len(canon)} modes)"
    return False, (f"DRIFT — update _EMBEDDED_ROUTES to match routes.json. "
                   f"canonical-only={canon.keys() - emb.keys()} embedded-only={emb.keys() - canon.keys()} "
                   f"changed={set(k for k in canon.keys() & emb.keys() if canon[k] != emb[k])}")


# --------------------------------------------------------------------------- HTTP
def _http_post(payload: dict, timeout: int) -> tuple[str | None, float]:
    key = env("OPENROUTER_API_KEY")
    if not key:
        return None, 0.0
    try:
        import requests  # lazy — only the actual-call path needs it
    except ModuleNotFoundError:
        print("[model-eval-gate] `requests` not installed — run `pip install -r requirements.txt`", file=sys.stderr)
        return None, 0.0
    payload = {**payload, "usage": {"include": True}}
    try:
        r = requests.post(_OR_HTTP, headers={"Authorization": f"Bearer {key}",
                                             "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        print(f"[model-eval-gate] OpenRouter call failed ({e.__class__.__name__})", file=sys.stderr)
        return None, 0.0
    if r.status_code != 200:
        print(f"[model-eval-gate] OpenRouter HTTP {r.status_code}: {r.text[:160]}", file=sys.stderr)
        return None, 0.0
    try:
        data = r.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        return None, 0.0
    usd = float((data.get("usage") or {}).get("cost") or 0.0)  # OpenRouter-reported cost
    return (text or None), usd


def text_call(mode: str, instruction: str, stdin_text: str = "", *, timeout: int = 180):
    if not env("OPENROUTER_API_KEY"):
        return None, 0.0
    model = _model_for(mode)  # refuses off-allowlist
    content = f"{instruction}\n\n{stdin_text[:_HTTP_TEXT_CAP]}" if stdin_text else instruction
    payload = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": content}]}
    prov = _provider_http(_provider_for(mode))
    if prov:
        payload["provider"] = prov
    return _http_post(payload, timeout)


def vision_call(mode: str, prompt: str, image_path, *, timeout: int = 180):
    if not env("OPENROUTER_API_KEY"):
        return None, 0.0
    model = _model_for(mode)
    try:
        b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    except Exception:  # noqa: BLE001
        return None, 0.0
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
    }
    prov = _provider_http(_provider_for(mode))
    if prov:
        payload["provider"] = prov
    return _http_post(payload, timeout)


# --------------------------------------------------------------------------- offline validation
def _validate_routes(raw) -> list[str]:
    """Python-side mirror of src/schema.ts validation (structural + cross-checks)."""
    errs: list[str] = []
    if not isinstance(raw, dict):
        return ["routes.json: root must be an object"]
    modes = raw.get("modes")
    if not isinstance(modes, dict) or not modes:
        return ["modes: at least one mode is required"]
    retired = raw.get("retired") or {}
    for name, m in modes.items():
        if name.lower() in _RESERVED_GENERIC:
            errs.append(f"modes.{name}: reserved generic name — name a mode for its task shape (GOVERNANCE.md)")
        if not isinstance(m, dict):
            errs.append(f"modes.{name}: must be an object"); continue
        for f in ("model", "purpose", "use_when", "do_not_use_when"):
            if not isinstance(m.get(f), str) or not m.get(f):
                errs.append(f"modes.{name}.{f}: required non-empty string")
        for f in ("priceIn", "priceOut"):
            if not isinstance(m.get(f), (int, float)):
                errs.append(f"modes.{name}.{f}: required number")
        vd = m.get("verified_date")
        if not (isinstance(vd, str) and _ISO_DATE.match(vd)):
            errs.append(f"modes.{name}.verified_date: must be an ISO date (YYYY-MM-DD)")
        if name in retired:
            errs.append(f'"{name}" appears in BOTH modes and retired')
    return errs


def _validate_spec(raw, mode_names: set[str]) -> list[str]:
    errs: list[str] = []
    if not isinstance(raw, dict):
        return ["spec: root must be an object"]
    if not isinstance(raw.get("mode"), str):
        errs.append("spec.mode: required string")
    elif mode_names and raw["mode"] not in mode_names:
        errs.append(f'spec targets mode "{raw["mode"]}" not in routes.json modes')
    if not isinstance(raw.get("input_template"), str) or not raw.get("input_template"):
        errs.append("spec.input_template: required non-empty string")
    graders = raw.get("graders")
    if not isinstance(graders, list) or not graders:
        errs.append("spec.graders: required non-empty array")
    else:
        for g in graders:
            if not isinstance(g, dict) or g.get("kind") not in _GRADER_KINDS:
                errs.append(f"spec.graders: unknown grader kind {g.get('kind') if isinstance(g, dict) else g!r}")
    if not isinstance(raw.get("tasks"), list) or not raw.get("tasks"):
        errs.append("spec.tasks: required non-empty array")
    return errs


def check() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    raw = None
    try:
        raw = json.loads(ROUTES_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        errors.append(f"routes.json: could not read/parse — {e}")
    mode_names: set[str] = set()
    if raw is not None:
        rerrs = _validate_routes(raw)
        if rerrs:
            errors += [f"routes.json: {e}" for e in rerrs]
        else:
            mode_names = set(raw["modes"])
            print(f"✓ routes.json — {len(mode_names)} modes, {len(raw.get('retired') or {})} retired")
    covered: set[str] = set()
    if SPEC_DIR.is_dir():
        for p in sorted(SPEC_DIR.glob("*.json")):
            try:
                sraw = json.loads(p.read_text())
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.name}: unparseable — {e}"); continue
            serrs = _validate_spec(sraw, mode_names)
            if serrs:
                errors += [f"{p.name}: {e}" for e in serrs]; continue
            covered.add(sraw["mode"])
            print(f"✓ {p.name} — mode \"{sraw['mode']}\", {len(sraw['tasks'])} tasks, {len(sraw['graders'])} grader(s)")
    for n in mode_names:
        if n not in covered:
            warnings.append(f'mode "{n}" has no regression spec — its verified permission isn\'t being re-checked')
    ok, cmsg = routes_consistency()
    if not ok:
        errors.append(f"embedded-fallback consistency: {cmsg}")
    print("")
    for w in warnings:
        print(f"⚠ {w}")
    if errors:
        print(f"\n✗ {len(errors)} policy error(s):")
        for e in errors:
            print(f"    - {e}")
        return 1
    print(f"\n✓ policy OK{f' ({len(warnings)} warning(s))' if warnings else ''}.")
    return 0


def explain(mode: str) -> int:
    md = _mode_def(mode)
    if md is None:
        retired = (_FULL or {}).get("retired", {}) if _FULL else {}
        if mode in retired:
            print(f"{mode}: RETIRED {retired[mode].get('retired_date','')} — {retired[mode].get('reason','')}")
            return 0
        print(f"✗ {mode!r} is not a known mode. Run `check` or `help`.", file=sys.stderr)
        return 2
    print(f"\n{mode}")
    print(f"  model:        {md['model']}")
    print(f"  purpose:      {md['purpose']}")
    print(f"  use when:     {md['use_when']}")
    print(f"  do NOT use:   {md['do_not_use_when']}")
    print(f"  evidence:     {md.get('evidence_ref', '(none)')}")
    print(f"  verified:     {md.get('verified_date')}")
    print(f"  provider pin: {json.dumps(md['provider']) if md.get('provider') else 'none'}")
    print(f"  constraints:  {json.dumps(md['constraints']) if md.get('constraints') else 'none (advisory only)'}\n")
    return 0


# --------------------------------------------------------------------------- CLI
def _cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("Usage: python src/router.py <mode> <prompt> [--rows N --stakes low|medium|high]")
        print("                                             [--input text|image --single-row --human-reviewed]")
        print("       python src/router.py check | explain <mode>\n")
        print("Allowed modes (see GOVERNANCE.md):")
        for k in sorted(_ROUTES):
            print(f"  {k:20s} -> {_ROUTES[k]['model']}")
        return 0
    cmd = argv[0].lower().strip()
    if cmd == "check":
        return check()
    if cmd == "explain":
        return explain((argv[1] if len(argv) > 1 else "").strip())

    mode = cmd
    if mode not in _ROUTES:
        print(f"✗ Mode {mode!r} is not on the allowlist. Run `python src/router.py help`.", file=sys.stderr)
        return 2
    # parse flags + prompt
    meta: dict = {}
    rest: list[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--rows": i += 1; meta["rows"] = int(argv[i])
        elif a == "--stakes": i += 1; meta["stakes"] = argv[i]
        elif a == "--input": i += 1; meta["input_type"] = argv[i]
        elif a == "--single-row": meta["single_row_decision"] = True
        elif a == "--human-reviewed": meta["human_review"] = True
        else: rest.append(a)
        i += 1
    pf = can_delegate(mode, meta)
    if not pf["ok"]:
        print(f"✗ Mode {mode!r} is allowed, but the task breaks its constraints:", file=sys.stderr)
        for v in pf["violations"]:
            print(f"    - {v}", file=sys.stderr)
        return 2
    for u in pf["unchecked"]:
        print(f"⚠ unchecked constraint: {u}", file=sys.stderr)
    prompt = " ".join(rest).strip()
    if not prompt:
        print(f"✗ Mode {mode!r} requires a prompt.", file=sys.stderr)
        return 1
    text, usd = text_call(mode, prompt)
    if text is None:
        print("✗ call failed (missing OPENROUTER_API_KEY or upstream error)", file=sys.stderr)
        return 1
    print(text)
    print(f"─── ${usd:.4f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
