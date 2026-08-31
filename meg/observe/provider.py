"""Provider-side collector — the authoritative money and routing record.

The daily activity rollup is the only source that is authoritative for spend, and
it is the only cross-check on whether the recorder is seeing everything. If the
provider says 26,000 requests and the recorder holds 4,000, the difference is
work that bypassed the gate -- see `Store.coverage()`.

**IT RETAINS 30 DAYS.** Older data is gone from the vendor permanently, so a
missed sync is unrecoverable. That single fact is why this ships as a scheduled
job with a loud failure path rather than an on-demand report: by the time anyone
notices a quiet failure, the window has eaten the evidence.

Key asymmetry worth knowing before debugging a 403/404: the daily rollup requires
a MANAGEMENT key; the per-generation record (see recorder.enrich) is readable by
the INFERENCE key. They are different credentials and neither substitutes.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ..store import Store

ACTIVITY_URL = "https://openrouter.ai/api/v1/activity"
KEYS_URL = "https://openrouter.ai/api/v1/keys"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"


def _get(url: str, key: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode(errors="replace")


def sync_activity(management_key: str, store: Store | None = None) -> dict:
    """Pull the day x model x endpoint rollup into `spend_daily`. Idempotent.

    The provider restates recent days as they settle, so rows upsert on
    (date, model, endpoint) -- a re-run corrects yesterday's partial numbers
    instead of double-counting them.
    """
    store = store or Store()
    code, body = _get(ACTIVITY_URL, management_key)
    if code == 403:
        raise RuntimeError(
            "403 from /activity — this endpoint requires a MANAGEMENT key. A normal "
            "inference key cannot read it. Mint one in your provider dashboard.")
    if code != 200:
        raise RuntimeError(f"/activity returned {code}: {str(body)[:200]}")

    rows = body.get("data") or []
    if not rows:
        # Treated as failure, not a quiet zero: a genuine no-usage account is
        # indistinguishable here from a broken credential, and guessing wrong in
        # the reassuring direction is how you lose a month of history.
        raise RuntimeError("/activity returned no rows — refusing to treat as success")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recs = [{
        "usage_date": r["date"][:10],
        "model": r.get("model") or r["model_permaslug"],
        "endpoint_id": r.get("endpoint_id") or "",
        "provider_name": r.get("provider_name"),
        "requests": r.get("requests", 0),
        "tokens_prompt": r.get("prompt_tokens", 0),
        "tokens_completion": r.get("completion_tokens", 0),
        "tokens_reasoning": r.get("reasoning_tokens", 0),
        "cost_usd": float(r.get("usage") or 0),
        "synced_at": now,
    } for r in rows]
    n = store.upsert_spend(recs)

    days = sorted({r["usage_date"] for r in recs})
    ledger = store.q("SELECT min(usage_date) lo, max(usage_date) hi, "
                     "count(*) n, sum(cost_usd) usd FROM spend_daily")[0]
    gap = bool(ledger["lo"] and str(ledger["lo"]) > days[0])
    return {
        "rows": n,
        "window": [days[0], days[-1]],
        "window_usd": round(sum(r["cost_usd"] for r in recs), 4),
        "ledger_rows": ledger["n"],
        "ledger_span": [str(ledger["lo"]), str(ledger["hi"])],
        # True means we started collecting later than data the vendor still holds,
        # i.e. history was already lost before this ran. Surfaced, never inferred.
        "history_gap": gap,
    }


def account_summary(management_key: str, inference_key: str | None = None) -> dict:
    """Lifetime totals + per-key usage. Useful for 'where is the spend actually going'.

    Multiple keys are the common case (a prod key, a test key, a teammate's key),
    and per-key usage is the fastest way to find spend nobody has attributed.
    """
    out: dict = {}
    code, body = _get(CREDITS_URL, inference_key or management_key)
    if code == 200:
        out["lifetime"] = body.get("data", {})
    code, body = _get(KEYS_URL, management_key)
    if code == 200:
        out["keys"] = [{
            "name": k.get("name"), "label": k.get("label"),
            "usage": k.get("usage"), "limit": k.get("limit"),
            "disabled": k.get("disabled"),
        } for k in (body.get("data") or [])]
    elif code == 403:
        out["keys_error"] = "requires a management key"
    return out
