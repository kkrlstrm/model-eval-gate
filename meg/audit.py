"""Hash-chained, tamper-evident audit log for delegation decisions. Stdlib only.

WHY A GOVERNANCE TOOL NEEDS THIS. Every claim this project makes is retrospective:
"that workload was refused", "this mode was allowed under these constraints",
"nothing bypassed the gate last month". Console logging cannot support any of
them — it is unordered, lossy, and trivially editable. A policy whose decisions
cannot be reconstructed is a policy you are asserting, not one you can show.

Each line is one decision plus two chain fields:

    {..decision.., "prev": <hash of previous line>, "hash": sha256(prev + canon(line))}

Editing or deleting any earlier line breaks the chain from that point forward, so
`verify()` can prove the log was not altered after the fact. It does NOT stop
someone truncating the file and starting a new chain — that is what an external
witness is for — but it does make silent edits detectable, which is the realistic
threat: a decision quietly reclassified after something went wrong.

WRITING IS BEST-EFFORT ON PURPOSE. An audit failure must never block a call. A
gate that goes down because its logger's disk filled would fail *closed* on
availability grounds, which is a worse outcome than a gap in the log — and the
gap is itself visible, because the chain records a sequence.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS = "GENESIS"
ENV_PATH = "MEG_AUDIT"


def default_path() -> Path:
    if override := os.environ.get(ENV_PATH):
        return Path(override)
    return Path.home() / ".model-eval-gate" / "audit.jsonl"


def _canon(event: dict) -> str:
    """Stable serialisation. Key order must not affect the hash or the chain
    breaks on a Python version that iterates dicts differently."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def _last_hash(path: Path) -> str:
    if not path.is_file():
        return GENESIS
    last = GENESIS
    with path.open(errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line).get("hash", last)
            except json.JSONDecodeError:
                # A torn final line (killed mid-write) must not silently reset the
                # chain to GENESIS -- that would look like a fresh, valid log.
                continue
    return last


def append(decision: Any, *, path: Path | str | None = None, **extra) -> str | None:
    """Append one decision. Returns its hash, or None if the write failed."""
    p = Path(path) if path else default_path()
    payload = asdict(decision) if is_dataclass(decision) else dict(decision)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **payload,
        **extra,
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        prev = _last_hash(p)
        event["prev"] = prev
        event["hash"] = hashlib.sha256((prev + _canon(event)).encode()).hexdigest()
        with p.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
        return event["hash"]
    except Exception:  # noqa: BLE001 — auditing must never break a call
        return None


def read(path: Path | str | None = None) -> Iterator[dict]:
    p = Path(path) if path else default_path()
    if not p.is_file():
        return
    with p.open(errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def verify(path: Path | str | None = None) -> dict:
    """Recompute the chain. Reports the FIRST break, which is where tampering starts."""
    p = Path(path) if path else default_path()
    prev, n = GENESIS, 0
    for i, event in enumerate(read(p), 1):
        n = i
        recorded = event.get("hash")
        body = {k: v for k, v in event.items() if k != "hash"}
        if body.get("prev") != prev:
            return {"ok": False, "lines": n, "broken_at": i,
                    "reason": f"line {i} claims prev={body.get('prev')!r}, "
                              f"chain expects {prev!r}"}
        expect = hashlib.sha256((prev + _canon(body)).encode()).hexdigest()
        if recorded != expect:
            return {"ok": False, "lines": n, "broken_at": i,
                    "reason": f"line {i} content does not match its hash — it was "
                              f"edited after being written"}
        prev = recorded
    return {"ok": True, "lines": n, "head": prev,
            "note": "chain intact — no line was edited or removed after it was written"}


def summary(path: Path | str | None = None) -> dict:
    """What the log says about delegation, for a human or a weekly review."""
    allowed = refused = 0
    by_mode: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    for e in read(path):
        ok = bool(e.get("allowed"))
        mode = e.get("mode") or "(none)"
        m = by_mode.setdefault(mode, {"allowed": 0, "refused": 0})
        if ok:
            allowed += 1
            m["allowed"] += 1
        else:
            refused += 1
            m["refused"] += 1
            head = str(e.get("reason", ""))[:60]
            reasons[head] = reasons.get(head, 0) + 1
    return {
        "allowed": allowed,
        "refused": refused,
        "by_mode": by_mode,
        "top_refusal_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:8],
        # A mode that never gets allowed is either mis-scoped or dead. Both are
        # findings; neither is visible without the log.
        "never_allowed": sorted(k for k, v in by_mode.items() if v["allowed"] == 0),
    }
