"""Harness adapters — read what work you actually do, from the agent you already run.

TWO HARNESSES, TWO MECHANISMS, ONE SCHEMA. This mirrors the split proven by
cc-logger and codex-logger, because the two products expose telemetry
differently and pretending otherwise loses data:

  Claude Code  emits lifecycle events for EVERY tool (PreToolUse / PostToolUse /
               SessionStart / Stop) and also writes a local transcript JSONL. The
               hook path is live; the transcript path is a backfill that works
               retroactively and needs no configuration -- important, because
               most people install this AFTER doing the work they want analysed.

  Codex        fires hooks only for shell commands, so hooks would silently miss
               file edits, MCP calls and sub-agents. Instead we tail the
               append-only rollout JSONL under ~/.codex/sessions/**, which
               already records every session, turn, tool call, model and per-turn
               token count. Hook-independent by design.

Both land in `work_events` with the same columns, so everything downstream --
clustering, proposal, scaffolding -- is harness-agnostic. Adding a third harness
means adding a reader here and nothing else.

WHAT THIS IS FOR. The provider knows what models you called; it has no idea what
you were DOING. Task shape, tool sequences, repeated sub-agent prompts, where
failures cluster -- that lives here, and it is what makes a workload class
describable in the first place.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..store import Store

CC_TRANSCRIPTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


def _eid(*parts: str) -> str:
    """Deterministic event id so re-ingesting a file is idempotent, not duplicative."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _iso(v) -> str:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1),
                                      timezone.utc).isoformat(timespec="seconds")
    return str(v or datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open(errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # A rollout/transcript being appended to right now can end in a
                    # partial line. Skipping it is correct; the next pass gets it.
                    continue
    except FileNotFoundError:
        return


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
def read_claude_code(root: Path | None = None, *, billing: str = "subscription"
                     ) -> list[dict]:
    """Parse Claude Code transcript JSONL into work_events.

    Backfill-friendly: reads what is already on disk, so a user gets a workload
    picture from months of past sessions the moment they install, without having
    configured a hook beforehand.
    """
    root = root or CC_TRANSCRIPTS
    out: list[dict] = []
    for path in sorted(Path(root).rglob("*.jsonl")):
        session = path.stem
        for i, rec in enumerate(_iter_jsonl(path)):
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name") or "?"
                inp = block.get("input") or {}
                out.append({
                    "event_id": _eid("cc", session, i, block.get("id") or name),
                    "harness": "claude-code",
                    "session_id": session,
                    "started_at": _iso(rec.get("timestamp")),
                    "kind": "tool_call",
                    "name": name,
                    "status": None,
                    "duration_ms": None,
                    "agent_model": msg.get("model"),
                    "subagent": inp.get("subagent_type"),
                    "payload": _summarize_tool(name, inp),
                    "billing": billing,
                })
    return out


def hook_event(body: dict, *, billing: str = "subscription") -> dict:
    """Map one Claude Code lifecycle hook POST into a work_event.

    The live counterpart to read_claude_code(). Same schema, so a user can run
    either or both without the downstream stages caring which produced a row.
    """
    name = body.get("tool_name") or body.get("hook_event_name") or "?"
    inp = body.get("tool_input") or {}
    return {
        "event_id": _eid("cc-hook", body.get("session_id", ""),
                         body.get("tool_use_id") or body.get("timestamp") or "", name),
        "harness": "claude-code",
        "session_id": body.get("session_id") or "unknown",
        "started_at": _iso(body.get("timestamp")),
        "kind": "tool_call",
        "name": name,
        "status": body.get("status"),
        "duration_ms": body.get("duration_ms"),
        "agent_model": body.get("model"),
        "subagent": inp.get("subagent_type"),
        "payload": _summarize_tool(name, inp),
        "billing": billing,
    }


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
def read_codex(root: Path | None = None, *, billing: str = "subscription") -> list[dict]:
    """Parse Codex rollout JSONL into work_events.

    Codex's hooks only cover shell commands, so this reads the rollout files
    directly -- the same choice codex-logger makes, and for the same reason:
    hook coverage would silently omit edits, MCP calls and sub-agents, and a
    workload picture with a hole in it is worse than none.
    """
    root = root or CODEX_SESSIONS
    out: list[dict] = []
    for path in sorted(Path(root).rglob("*.jsonl")):
        session = path.stem
        for i, rec in enumerate(_iter_jsonl(path)):
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
            # Codex wraps the interesting record: the envelope is
            # {"type": "response_item", "payload": {"type": "custom_tool_call", ...}}.
            # Reading only the envelope type yields turn_context rows and silently
            # drops every tool call -- which is exactly the under-collection this
            # module's docstring warns about, so the inner type wins when present.
            typ = payload.get("type") or rec.get("type") or ""

            if typ in ("function_call", "local_shell_call", "custom_tool_call"):
                name = (payload.get("name") or payload.get("tool_name")
                        or typ.replace("_call", ""))
                # Three shapes in the wild: `arguments` as a JSON string
                # ({"cmd": ...}), `action` as an object, and `input` as raw text
                # (apply_patch ships a literal patch, not JSON).
                args = (payload.get("arguments") or payload.get("action")
                        or payload.get("input") or {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"command": args}
                if not isinstance(args, dict):
                    args = {}
                out.append({
                    "event_id": _eid("codex", session, i, name),
                    "harness": "codex",
                    "session_id": session,
                    "started_at": _iso(rec.get("timestamp")),
                    "kind": "tool_call",
                    "name": name,
                    "status": None,
                    "duration_ms": None,
                    "agent_model": None,
                    "subagent": None,
                    "payload": _summarize_tool(name, args if isinstance(args, dict) else {}),
                    "billing": billing,
                })
            elif typ == "turn_context":
                out.append({
                    "event_id": _eid("codex", session, i, "turn"),
                    "harness": "codex",
                    "session_id": session,
                    "started_at": _iso(rec.get("timestamp")),
                    "kind": "turn",
                    "name": payload.get("model"),
                    "status": None,
                    "duration_ms": None,
                    "agent_model": payload.get("model"),
                    "subagent": None,
                    "payload": {"cwd": payload.get("cwd")},
                    "billing": billing,
                })
            elif typ == "token_count":
                # Codex reports per-turn token usage the provider never sees as a
                # discrete call. It is the only place a subscription-billed turn's
                # size is recorded, which the counterfactual needs.
                # `info` is legitimately null on rate-limit-only records; emitting
                # those would pad the table with all-None rows that look like data.
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                last = info.get("last_token_usage") or {}
                out.append({
                    "event_id": _eid("codex", session, i, "tokens"),
                    "harness": "codex",
                    "session_id": session,
                    "started_at": _iso(rec.get("timestamp")),
                    "kind": "tokens",
                    "name": None,
                    "status": None,
                    "duration_ms": None,
                    "agent_model": None,
                    "subagent": None,
                    "payload": {
                        "input": last.get("input_tokens"),
                        "cached": last.get("cached_input_tokens"),
                        "output": last.get("output_tokens"),
                        "reasoning": last.get("reasoning_output_tokens"),
                    },
                    "billing": billing,
                })
    return out


# --------------------------------------------------------------------------- #
def _summarize_tool(name: str, inp: dict) -> dict:
    """Keep the SHAPE of a call, drop the content.

    Clustering needs to know 'a bash call that ran page-digest.py on a URL', not
    the URL. Storing full arguments would put customer data, credentials and
    prompt text into a telemetry DB that a user may later share when asking for
    routing help -- so the summary is deliberately lossy, and content never lands.
    """
    out: dict = {}
    if cmd := (inp.get("command") or inp.get("cmd")):
        if isinstance(cmd, list):        # some harnesses pass argv, not a string
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd)
        out["argv0"] = cmd.strip().split()[0] if cmd.strip() else ""
        for tok in cmd.split():
            if tok.endswith((".py", ".sh", ".ts", ".js")):
                out["script"] = os.path.basename(tok)
                break
        out["len"] = len(cmd)
    for k in ("file_path", "path", "notebook_path"):
        if v := inp.get(k):
            out["ext"] = os.path.splitext(str(v))[1]
            break
    if p := inp.get("prompt"):
        # A prompt fingerprint clusters repeated sub-agent work without keeping text.
        out["prompt_sha"] = hashlib.sha256(str(p).encode()).hexdigest()[:12]
        out["prompt_len"] = len(str(p))
    if q := (inp.get("query") or inp.get("pattern")):
        out["query_len"] = len(str(q))
    return out


def ingest(store: Store | None = None, *, claude_code: bool = True,
           codex: bool = True, cc_root: Path | None = None,
           codex_root: Path | None = None) -> dict:
    """Read every configured harness into the store. Idempotent."""
    store = store or Store()
    stats = {}
    if claude_code:
        rows = read_claude_code(cc_root)
        stats["claude_code"] = store.upsert_work(rows)
    if codex:
        rows = read_codex(codex_root)
        stats["codex"] = store.upsert_work(rows)
    return stats
