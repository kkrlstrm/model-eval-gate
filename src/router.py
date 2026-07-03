#!/usr/bin/env python3
"""
model-eval-gate (Python port) — the same eval-gated allowlist, callable from Python.

Every non-frontier delegation goes through ONE model-routing allowlist. The CLI
enforcer (src/cli.ts) is the interactive front door; this module is the library
equivalent so the SAME allowlist travels with a distributed copy that doesn't
ship Node/tsx. It maps each approved mode to exactly one verified model, refuses
anything else, and calls OpenRouter directly over HTTPS.

The allowlist is read live from `routes.json` (the single source of truth). A
minimal embedded fallback carries the enforcement essentials (mode -> model +
provider pin) for a copy shipped WITHOUT routes.json; `routes_consistency()`
asserts the two never diverge when both are present.

Every call is best-effort: it returns (None, cost) on any failure so callers can
fall back deterministically.

Public API:
    text_call(mode, instruction, stdin_text, timeout=180) -> (text|None, usd)
    vision_call(mode, prompt, image_path, timeout=180)    -> (text|None, usd)

    python src/router.py <mode> "<prompt>"          # also works as a CLI
    python src/router.py help
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ROUTES_PATH = ROOT / "routes.json"
_OR_HTTP = "https://openrouter.ai/api/v1/chat/completions"
_HTTP_TEXT_CAP = 400_000  # cap the payload sanely even on 1M-context models

# Minimal embedded fallback — mode -> {model, provider}. Only used when routes.json
# is absent (a distributed copy). Keep in sync with routes.json; routes_consistency()
# will flag drift. New modes require a passing eval per GOVERNANCE.md.
_EMBEDDED_ROUTES: dict[str, dict] = {
    "extract-bulk":       {"model": "qwen/qwen3-235b-a22b-2507",     "provider": None},
    "filter-auto-reply":  {"model": "google/gemini-2.5-flash-lite",  "provider": None},
    "lint-code":          {"model": "mistralai/codestral-2508",      "provider": None},
    "digest-longcontext": {"model": "deepseek/deepseek-v4-flash",    "provider": None},
    "extract-accurate":   {"model": "deepseek/deepseek-v4-flash",    "provider": None},
    "extract-multimodal": {"model": "minimax/minimax-m3",            "provider": None},
}


def env(name: str) -> str:
    """OPENROUTER_API_KEY etc. — process env first, then a .env at the repo root."""
    v = os.environ.get(name)
    if v:
        return v
    envfile = ROOT / ".env"
    if envfile.is_file():
        for line in envfile.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _load_routes() -> dict[str, dict]:
    """mode -> {model, provider}. Canonical routes.json when present, else embedded."""
    try:
        raw = json.loads(ROUTES_PATH.read_text())
        modes = raw.get("modes") or {}
        if modes:
            return {k: {"model": v["model"], "provider": v.get("provider")}
                    for k, v in modes.items()}
    except Exception:  # noqa: BLE001 — any read/parse issue falls back to embedded
        pass
    return _EMBEDDED_ROUTES


_ROUTES = _load_routes()


def _model_for(mode: str) -> str:
    try:
        return _ROUTES[mode]["model"]
    except KeyError:
        raise ValueError(
            f"model-eval-gate: mode {mode!r} is not on the routing allowlist "
            f"({', '.join(sorted(_ROUTES))}). New modes require a passing eval "
            f"per GOVERNANCE.md."
        )


def _provider_for(mode: str) -> dict | None:
    return (_ROUTES.get(mode) or {}).get("provider")


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


def routes_consistency() -> tuple[bool, str]:
    """Assert the embedded fallback matches canonical routes.json when both exist.
    Returns (ok, message)."""
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
                   f"changed={{k for k in canon.keys() & emb.keys() if canon[k] != emb[k]}}")


def _http_post(payload: dict, timeout: int) -> tuple[str | None, float]:
    key = env("OPENROUTER_API_KEY")
    if not key:
        return None, 0.0
    payload = {**payload, "usage": {"include": True}}  # ask OpenRouter to return $ cost
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
    usd = float((data.get("usage") or {}).get("cost") or 0.0)
    return (text or None), usd


def text_call(mode: str, instruction: str, stdin_text: str = "", *, timeout: int = 180):
    """Delegate a text task to the mode's allowlisted model. Returns (text|None, usd)."""
    if not env("OPENROUTER_API_KEY"):
        return None, 0.0
    model = _model_for(mode)  # also refuses an off-allowlist mode
    content = f"{instruction}\n\n{stdin_text[:_HTTP_TEXT_CAP]}" if stdin_text else instruction
    payload = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": content}]}
    prov = _provider_http(_provider_for(mode))
    if prov:
        payload["provider"] = prov
    return _http_post(payload, timeout)


def vision_call(mode: str, prompt: str, image_path, *, timeout: int = 180):
    """Delegate a page-image task to the mode's allowlisted vision model. Returns (text|None, usd)."""
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


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("Usage: python src/router.py <mode> <prompt>\n")
        print("Allowed modes (see GOVERNANCE.md):")
        for k in sorted(_ROUTES):
            print(f"  {k:20s} -> {_ROUTES[k]['model']}")
        return 0
    mode = argv[0].lower().strip()
    if mode not in _ROUTES:
        print(f"✗ Mode {mode!r} is not on the allowlist. Run `python src/router.py help`.", file=sys.stderr)
        return 2
    prompt = " ".join(argv[1:]).strip()
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
