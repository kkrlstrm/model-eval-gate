"""Read RunTune's evidence instead of re-reading the harnesses.

RunTune already reads every coding agent this project cares about (Claude Code, Codex,
Cursor, Antigravity), a telemetry warehouse, and the router's call log, and it keeps the
redacted command text a workload class is described by. Re-reading the same transcripts
here with a second set of parsers would only produce a second, slightly different copy
of the same evidence. So `meg observe ingest --from-runtune` loads RunTune's corpus and
writes it into this store:

    tool attempts     -> work_events   (payload: argv0, script, shape, redacted text)
    sub-agents        -> work_events   (one row per agent, named by its task: the
                                        workflow and phase, or the Agent call's
                                        description; payload.task + prompt_sha)
    router calls      -> calls         (mode, model, tokens, cost, latency; billing api)

The harness readers in harness.py stay for use without RunTune.
"""
from __future__ import annotations

import hashlib
import os

from ..store import Store


def _id(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def rows_from_corpus(corpus, host_billing: str = "subscription") -> tuple[list[dict], list[dict]]:
    """(work_events rows, calls rows) from a RunTune Corpus. Pure."""
    work, calls = [], []
    # Two attempts can share every recorded field (parallel calls of one command in one
    # second). Numbering each repeat keeps them two rows; the same corpus always yields
    # the same set of ids, so re-ingesting stays idempotent.
    seen: dict[tuple, int] = {}

    def occurrence(*key) -> int:
        seen[key] = seen.get(key, -1) + 1
        return seen[key]

    for e in corpus.events:
        ts = e.ts.isoformat(timespec="seconds")
        if e.kind == "model":
            calls.append({
                "generation_id": _id("runtune", e.source, e.session, ts, e.surface, e.model, e.text,
                                     occurrence("call", e.source, e.session, ts, e.surface, e.model, e.text)),
                "created_at": ts, "workload": e.surface, "mode": e.surface, "model": e.model or "?",
                "tokens_prompt": e.tokens_in, "tokens_completion": e.tokens_out,
                "cost_usd": e.usd, "latency_ms": e.duration_ms,
                "billing": getattr(e, "billing", None) or "api", "source": "runtune",
                "meta": {"caller": e.actor, "label": e.text, "ok": e.ok},
            })
            continue
        toks = (e.shape or "").split(" ")
        payload = {"argv0": toks[0] if toks else "", "shape": e.shape, "len": e.chars}
        script = next((t for t in toks if t.endswith((".py", ".sh", ".ts", ".js"))), None)
        if script:
            payload["script"] = os.path.basename(script)
        if e.text:
            payload["text"] = e.text                 # already redacted by RunTune
        if e.inline_target:
            payload["inline_target"] = e.inline_target
        if not e.ok and e.error:
            payload["error"] = e.error
        work.append({
            "event_id": _id("runtune", e.source, e.session, ts, e.surface, e.text,
                            occurrence("work", e.source, e.session, ts, e.surface, e.text)),
            "harness": e.source, "session_id": e.session, "started_at": ts,
            "kind": "tool_call", "name": e.surface,
            "status": "success" if e.ok else "failure", "duration_ms": e.duration_ms,
            "agent_model": e.model, "subagent": None if e.actor == "root" else e.actor,
            "payload": payload, "billing": getattr(e, "billing", None) or host_billing,
        })
    for inv in getattr(corpus, "invocations", []):
        task = getattr(inv, "task", None)
        if inv.agent_type == "root" or not task or not inv.started:
            continue
        # One row per agent, keyed by its task, so `from_harness` sees a sub-agent job
        # repeated across runs: the unit a bake-off would move to a cheaper model.
        payload = {"task": task, "prompt_sha": hashlib.sha256(task.encode()).hexdigest()[:12]}
        for k in ("description", "workflow_name", "workflow_phase"):
            if getattr(inv, k, None):
                payload[k] = getattr(inv, k)
        if getattr(inv, "prompt", None):
            payload["text"] = inv.prompt                  # already redacted by RunTune
        work.append({
            "event_id": _id("runtune-agent", inv.source, inv.invocation),
            "harness": inv.source, "session_id": inv.session,
            "started_at": inv.started.isoformat(timespec="seconds"),
            "kind": "tool_call", "name": "Agent",
            "status": "success" if inv.status == "completed" else "failure",
            "duration_ms": int((inv.ended - inv.started).total_seconds() * 1000) if inv.ended else None,
            "agent_model": inv.model, "subagent": inv.agent_type,
            "payload": payload, "billing": host_billing,
        })
    return work, calls


def ingest(store: Store | None = None, *, sources: list[str] | None = None, db: str | None = None,
           days: int = 30, routes: str | None = None, jsonl: list[str] | None = None,
           host_billing: str = "subscription") -> dict:
    """Load RunTune's corpus and write it into the store. Idempotent: ids are derived
    from the evidence, so re-ingesting the same window updates rows instead of
    duplicating them."""
    try:
        from runtune import sources as rt_sources
    except ImportError as exc:
        raise SystemExit("meg: --from-runtune needs RunTune installed (pip install runtune)") from exc
    if sources is None:
        warehouse = bool(db and ("://" in db or os.environ.get(db))) or bool(os.environ.get("RUNTUNE_DB_URL"))
        sources = ["claude", "codex", "cursor", "antigravity", "openrouter"] if warehouse else ["local"]
    corpus = rt_sources.load(sources, db or "RUNTUNE_DB_URL", days, routes, jsonl)
    work, calls = rows_from_corpus(corpus, host_billing)
    store = store or Store()
    return {"work_events": store.upsert_work(work), "calls": store.upsert_calls(calls),
            "coverage": {k: v.line() for k, v in corpus.coverage.items()}}
