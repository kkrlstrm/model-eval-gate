"""Unified telemetry store for model-eval-gate.

SQLite by default (zero setup, `~/.model-eval-gate/meg.db`); pass a
`postgresql://` URL to co-locate with an existing telemetry database. The schema
is identical either way, so a user can start on SQLite and move without
re-instrumenting.

THREE TABLES, THREE SOURCES — deliberately kept separate rather than merged:

  calls          one row per LLM call. Written by the recorder at call time
                 (which is the only moment the provider's generation id exists),
                 then enriched later from the provider's per-generation endpoint.
  spend_daily    the provider's own day x model rollup. Authoritative for money,
                 and the only cross-check on `calls` completeness -- if the
                 recorder is bypassed, these two disagree and that IS the signal.
  work_events    task-shape signal from the coding-agent harnesses (Claude Code,
                 Codex). This is what makes a workload class describable; the
                 provider knows nothing about it.

Nothing here is best-effort. A telemetry layer that silently swallows write
failures produces a ledger you cannot reason about, which is worse than no
ledger -- you would trust it. Callers that genuinely want to degrade should
catch explicitly.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_DB = Path.home() / ".model-eval-gate" / "meg.db"
ENV_DB = "MEG_DB"

# `?` for sqlite, `%s` for postgres -- the only dialect difference we care about.
SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    generation_id     TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    workload          TEXT,
    mode              TEXT,
    model             TEXT NOT NULL,
    provider_name     TEXT,
    endpoint_id       TEXT,
    tokens_prompt     INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0,
    tokens_reasoning  INTEGER DEFAULT 0,
    tokens_cached     INTEGER DEFAULT 0,
    cost_usd          REAL    DEFAULT 0,
    latency_ms        INTEGER,
    generation_ms     INTEGER,
    finish_reason     TEXT,
    streamed          INTEGER DEFAULT 0,
    had_schema        INTEGER DEFAULT 0,
    billing           TEXT,
    enriched          INTEGER DEFAULT 0,
    source            TEXT,
    meta              TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_workload ON calls (workload);
CREATE INDEX IF NOT EXISTS idx_calls_model    ON calls (model);
CREATE INDEX IF NOT EXISTS idx_calls_enriched ON calls (enriched);

CREATE TABLE IF NOT EXISTS spend_daily (
    usage_date        TEXT NOT NULL,
    model             TEXT NOT NULL,
    endpoint_id       TEXT NOT NULL,
    provider_name     TEXT,
    requests          INTEGER DEFAULT 0,
    tokens_prompt     INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0,
    tokens_reasoning  INTEGER DEFAULT 0,
    cost_usd          REAL    DEFAULT 0,
    synced_at         TEXT NOT NULL,
    PRIMARY KEY (usage_date, model, endpoint_id)
);
CREATE INDEX IF NOT EXISTS idx_spend_date ON spend_daily (usage_date);

CREATE TABLE IF NOT EXISTS work_events (
    event_id     TEXT PRIMARY KEY,
    harness      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    name         TEXT,
    status       TEXT,
    duration_ms  INTEGER,
    agent_model  TEXT,
    subagent     TEXT,
    payload      TEXT,
    billing      TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_session ON work_events (session_id);
CREATE INDEX IF NOT EXISTS idx_work_kind    ON work_events (kind);
CREATE INDEX IF NOT EXISTS idx_work_name    ON work_events (name);
"""


def _is_pg(dsn: str) -> bool:
    return dsn.startswith("postgres://") or dsn.startswith("postgresql://")


def resolve_db(db: str | None = None) -> str:
    return db or os.environ.get(ENV_DB) or str(DEFAULT_DB)


class Store:
    """Thin dialect-tolerant wrapper. Not an ORM; the schema is small on purpose."""

    def __init__(self, dsn: str | None = None):
        self.dsn = resolve_db(dsn)
        self.pg = _is_pg(self.dsn)
        if not self.pg:
            Path(self.dsn).parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if self.pg:
            import psycopg2  # imported lazily: SQLite users need no driver
            conn = psycopg2.connect(self.dsn)
        else:
            conn = sqlite3.connect(self.dsn)
            conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ddl(self) -> str:
        if not self.pg:
            return SCHEMA
        # Postgres has no INTEGER-as-bool coercion issue here, but REAL/TEXT map
        # cleanly, so the only rewrite needed is the autoincrement-free PKs (none).
        return SCHEMA

    def _ensure(self) -> None:
        with self._conn() as c:
            cur = c.cursor()
            for stmt in self._ddl().split(";"):
                if stmt.strip():
                    cur.execute(stmt)

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        """SELECT. `sql` uses `?` placeholders; they are rewritten for Postgres."""
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(sql.replace("?", "%s") if self.pg else sql, tuple(params))
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def exec_many(self, sql: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self._conn() as c:
            cur = c.cursor()
            cur.executemany(sql.replace("?", "%s") if self.pg else sql, rows)
        return len(rows)

    # ------------------------------------------------------------------ writes
    def upsert_calls(self, records: list[dict]) -> int:
        cols = ["generation_id", "created_at", "workload", "mode", "model",
                "provider_name", "endpoint_id", "tokens_prompt", "tokens_completion",
                "tokens_reasoning", "tokens_cached", "cost_usd", "latency_ms",
                "generation_ms", "finish_reason", "streamed", "had_schema",
                "billing", "enriched", "source", "meta"]
        ph = ",".join("?" * len(cols))
        # COALESCE, not plain assignment. A record arrives in two halves -- the
        # recorder knows `mode`/`workload`/`billing`/`had_schema`, the provider
        # knows the metered cost and which endpoint served it -- and neither half
        # carries the other's columns. With `SET c = excluded.c`, the second write
        # silently NULLs everything the first one established, destroying exactly
        # the attribution this table exists for. Nothing in this schema ever wants
        # to be set back to NULL, so COALESCE is the correct merge everywhere and
        # partial upserts become safe for any caller, not just careful ones.
        upd = ",".join(f"{c}=COALESCE(excluded.{c}, calls.{c})"
                       for c in cols if c != "generation_id")
        sql = (f"INSERT INTO calls ({','.join(cols)}) VALUES ({ph}) "
               f"ON CONFLICT (generation_id) DO UPDATE SET {upd}")
        rows = []
        for r in records:
            r = dict(r)
            if isinstance(r.get("meta"), (dict, list)):
                r["meta"] = json.dumps(r["meta"])
            rows.append(tuple(r.get(c) for c in cols))
        return self.exec_many(sql, rows)

    def upsert_spend(self, records: list[dict]) -> int:
        cols = ["usage_date", "model", "endpoint_id", "provider_name", "requests",
                "tokens_prompt", "tokens_completion", "tokens_reasoning",
                "cost_usd", "synced_at"]
        ph = ",".join("?" * len(cols))
        upd = ",".join(f"{c}=COALESCE(excluded.{c}, spend_daily.{c})" for c in cols
                       if c not in ("usage_date", "model", "endpoint_id"))
        sql = (f"INSERT INTO spend_daily ({','.join(cols)}) VALUES ({ph}) "
               f"ON CONFLICT (usage_date, model, endpoint_id) DO UPDATE SET {upd}")
        return self.exec_many(sql, [tuple(r.get(c) for c in cols) for r in records])

    def upsert_work(self, records: list[dict]) -> int:
        cols = ["event_id", "harness", "session_id", "started_at", "kind", "name",
                "status", "duration_ms", "agent_model", "subagent", "payload", "billing"]
        ph = ",".join("?" * len(cols))
        upd = ",".join(f"{c}=COALESCE(excluded.{c}, work_events.{c})" for c in cols if c != "event_id")
        sql = (f"INSERT INTO work_events ({','.join(cols)}) VALUES ({ph}) "
               f"ON CONFLICT (event_id) DO UPDATE SET {upd}")
        rows = []
        for r in records:
            r = dict(r)
            if isinstance(r.get("payload"), (dict, list)):
                r["payload"] = json.dumps(r["payload"])
            rows.append(tuple(r.get(c) for c in cols))
        return self.exec_many(sql, rows)

    # ------------------------------------------------------------------- reads
    def unenriched(self, limit: int = 500) -> list[dict]:
        return self.q("SELECT generation_id FROM calls WHERE enriched = 0 "
                      "ORDER BY created_at LIMIT ?", [limit])

    def coverage(self) -> dict:
        """Recorder coverage vs the provider's own rollup.

        A gap means calls are reaching the provider WITHOUT going through the
        recorder -- i.e. something bypassed the gate. This is the single most
        important health metric in the system, so it is a first-class read.
        """
        c = self.q("SELECT count(*) n, COALESCE(sum(cost_usd),0) usd FROM calls")[0]
        s = self.q("SELECT COALESCE(sum(requests),0) n, COALESCE(sum(cost_usd),0) usd "
                   "FROM spend_daily")[0]
        n_recorded, n_provider = int(c["n"] or 0), int(s["n"] or 0)
        return {
            "recorded_calls": n_recorded,
            "provider_requests": n_provider,
            "recorded_usd": round(float(c["usd"] or 0), 4),
            "provider_usd": round(float(s["usd"] or 0), 4),
            "untracked_requests": max(0, n_provider - n_recorded),
            "coverage_pct": round(100.0 * n_recorded / n_provider, 1) if n_provider else None,
        }
