"""The call recorder — instrumentation and enforcement in one object.

WHY THESE ARE THE SAME THING. A gate you can go around is a suggestion. Two
production scripts in the system this was extracted from called the provider with
a raw HTTP POST, on a model that had never been evaluated, while their own
comments claimed they used the allowlisted one. Nothing errored, and it was
invisible for months.

So the recorder is not a logging decorator bolted onto a router. It IS the egress
path: it refuses off-allowlist modes *and* writes the telemetry, in one call. You
cannot get the convenience without the accounting, and `store.coverage()` reports
any traffic that reached the provider without passing through here.

WHY IT CAPTURES THE GENERATION ID. The provider offers a rich per-call record (45
fields: real metered cost, cache discounts, the endpoint that actually served the
request, truncation reason, TTFT vs total latency) but there is **no endpoint that
lists your generations**. You can only look one up by id, and the id exists only
in the response body you are about to discard. Capture it here or the record is
unreachable forever.

WHY IT TAGS `external_user`. The provider echoes the request's `user` field back
on the generation record. Tagging every call with its workload turns a
model-shaped ledger ("what did model X cost this month") into a workload-shaped
one ("what did contact-lookup cost this month") -- the only shape a routing
decision can actually be made from.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..store import Store

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
GEN_URL = "https://openrouter.ai/api/v1/generation?id="
REFERER = "https://github.com/kkrlstrm/model-eval-gate"


class RefusedError(RuntimeError):
    """Raised when a mode is not on the allowlist, or its constraints fail.

    A distinct type so callers can tell 'the gate said no' (a policy decision,
    never retry) from 'the provider failed' (transient, retry is reasonable)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _post(url: str, payload: dict, key: str, timeout: int, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get(url: str, key: str, timeout: int = 30) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode(errors="replace")


class Recorder:
    """Gated, instrumented model egress.

    `router` is any object exposing `model_for(mode)` and, optionally,
    `can_delegate(mode, meta)` -- i.e. the existing gate. Injected rather than
    imported so the recorder stays testable without a live routes.json.
    """

    def __init__(self, router, api_key: str, store: Store | None = None,
                 *, billing: str = "api"):
        self.router = router
        self.key = api_key
        self.store = store or Store()
        # 'api' = a real invoice line. 'subscription' = already paid for, marginal
        # cost ~0. Recorded per call because the honest saving from delegating is
        # ONLY the api-billed portion, and a gate that conflates them reports
        # savings that do not exist.
        self.billing = billing

    # ------------------------------------------------------------------ egress
    def call(self, mode: str, messages: list[dict], *, workload: str,
             meta: dict | None = None, timeout: int = 180, **params) -> dict:
        """Run one gated, recorded call. Returns the provider response body.

        `workload` is the task-class tag (e.g. "contact-lookup/page-digest"). It
        is REQUIRED: an untagged call cannot be attributed later, and an
        unattributable call cannot inform a routing decision, so allowing one
        would quietly defeat the point of the recorder.
        """
        if not workload or not str(workload).strip():
            raise RefusedError(
                "workload tag is required — an untagged call cannot be attributed "
                "to a task class, and unattributable calls cannot inform routing.")

        model = self.router.model_for(mode)          # raises for off-allowlist modes
        check = getattr(self.router, "can_delegate", None)
        if check is not None:
            verdict = check(mode, meta or {})
            if not verdict.get("ok", True):
                raise RefusedError(
                    f"mode {mode!r} refused: {'; '.join(verdict.get('violations', []))}")

        payload = {"model": model, "messages": messages,
                   "user": f"{workload}", **params}
        provider_pin = getattr(self.router, "provider_for", lambda _m: None)(mode)
        if provider_pin:
            payload["provider"] = provider_pin

        t0 = time.time()
        body = _post(CHAT_URL, payload, self.key, timeout, {"HTTP-Referer": REFERER})
        wall_ms = int((time.time() - t0) * 1000)

        usage = body.get("usage") or {}
        gid = body.get("id")
        self.store.upsert_calls([{
            "generation_id": gid or f"local-{t0:.0f}",
            "created_at": _now(),
            "workload": workload,
            "mode": mode,
            "model": model,
            "provider_name": body.get("provider"),
            "endpoint_id": None,
            "tokens_prompt": usage.get("prompt_tokens", 0),
            "tokens_completion": usage.get("completion_tokens", 0),
            "tokens_reasoning": 0,
            "tokens_cached": 0,
            # Cost from the response is an estimate; enrichment replaces it with
            # the provider's metered figure, which is what actually gets billed.
            "cost_usd": 0.0,
            "latency_ms": wall_ms,
            "generation_ms": None,
            "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
            "streamed": 0,
            "had_schema": 1 if params.get("response_format") else 0,
            "billing": self.billing,
            "enriched": 0,
            "source": "recorder",
            "meta": meta or {},
        }])
        return body

    # -------------------------------------------------------------- enrichment
    def enrich(self, limit: int = 500, *, settle_s: float = 6.0,
               max_attempts: int = 3) -> dict:
        """Convenience wrapper around the module-level `enrich()`.

        Kept because enriching straight after a batch of calls is a natural thing
        to do on the object you just used. The real function takes no router,
        because backfilling cost is a READ — there is nothing to gate."""
        return enrich(self.store, self.key, limit=limit, settle_s=settle_s,
                      max_attempts=max_attempts)


def enrich(store: Store, api_key: str, *, limit: int = 500, settle_s: float = 6.0,
           max_attempts: int = 3) -> dict:
    """Backfill metered cost / provider / token detail from the provider.

    A FUNCTION, not a Recorder method, on purpose. Enrichment reads records that
    already exist; it makes no model call and therefore has no delegation to
    gate. Hanging it off the egress object forced callers who only wanted to
    backfill costs to construct a Recorder with a `router=None` it would never
    use — a fake dependency that would blow up the moment anyone called `.call()`
    on that instance. The dependency was never real, so it is gone.

    A generation is not queryable the instant it completes -- the first fetch
    reliably 404s for several seconds. That is eventual consistency, not a missing
    record, so a 404 is retried rather than marked dead.

    NOTE: this endpoint is readable by the INFERENCE key, not the management key
    -- the opposite of the daily-activity endpoint. A collector needs both.
    """
    pending = [r["generation_id"] for r in store.unenriched(limit)
               if not str(r["generation_id"]).startswith("local-")]
    done = missing = 0
    for gid in pending:
        rec = None
        for _attempt in range(max_attempts):
            code, body = _get(GEN_URL + gid, api_key)
            if code == 200:
                rec = body["data"]
                break
            if code == 404:
                time.sleep(settle_s)
                continue
            break
        if rec is None:
            missing += 1
            continue
        pr = (rec.get("provider_responses") or [{}])
        _merge_preserving(store, gid, {
            "generation_id": gid,
            "created_at": rec.get("created_at") or _now(),
            "workload": rec.get("external_user"),
            "mode": None,
            "model": rec.get("model"),
            "provider_name": rec.get("provider_name"),
            "endpoint_id": (pr[0] or {}).get("endpoint_id"),
            "tokens_prompt": rec.get("native_tokens_prompt") or 0,
            "tokens_completion": rec.get("native_tokens_completion") or 0,
            "tokens_reasoning": rec.get("native_tokens_reasoning") or 0,
            "tokens_cached": rec.get("native_tokens_cached") or 0,
            "cost_usd": float(rec.get("total_cost") or 0),
            "latency_ms": rec.get("latency"),
            "generation_ms": rec.get("generation_time"),
            "finish_reason": rec.get("finish_reason"),
            "streamed": 1 if rec.get("streamed") else 0,
            "had_schema": None,
            "billing": None,
            "enriched": 1,
            "source": "generation",
            "meta": {"native_finish_reason": rec.get("native_finish_reason"),
                     "cache_discount": rec.get("cache_discount"),
                     "service_tier": rec.get("service_tier")},
        })
        done += 1
    return {"pending": len(pending), "enriched": done, "unresolved": missing}


def _merge_preserving(store: Store, gid: str, patch: dict) -> None:
    """Enrichment must not blank fields only the recorder knows.

    `mode`, `workload`, `had_schema` and `billing` are local concepts the provider
    has never heard of. A naive upsert of the provider's record would null them,
    silently destroying exactly the attribution this system exists to create.
    """
    prior = store.q("SELECT * FROM calls WHERE generation_id = ?", [gid])
    if prior:
        for k in ("mode", "workload", "had_schema", "billing"):
            if patch.get(k) in (None, "") and prior[0].get(k) is not None:
                patch[k] = prior[0][k]
    store.upsert_calls([patch])
