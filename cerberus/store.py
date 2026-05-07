"""SQLite persistence: runs, check_results, manual_toggles, eta_history.

Synchronous sqlite3 wrapped behind asyncio.to_thread for FastAPI use.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cerberus.db"


def _now_ms() -> int:
    return int(time.time() * 1000)


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as cx:
        cx.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY,
              url TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at INTEGER NOT NULL,
              completed_at INTEGER,
              summary_json TEXT,
              error TEXT,
              environment TEXT NOT NULL DEFAULT 'production'
            );
            CREATE TABLE IF NOT EXISTS check_results (
              run_id TEXT NOT NULL,
              check_id TEXT NOT NULL,
              status TEXT NOT NULL,
              summary TEXT,
              details_json TEXT,
              started_at INTEGER,
              completed_at INTEGER,
              PRIMARY KEY (run_id, check_id),
              FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS manual_toggles (
              run_id TEXT NOT NULL,
              check_id TEXT NOT NULL,
              marked_status TEXT NOT NULL,
              marked_at INTEGER NOT NULL,
              PRIMARY KEY (run_id, check_id),
              FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS eta_history (
              check_id TEXT NOT NULL,
              runtime_ms INTEGER NOT NULL,
              recorded_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_eta_check ON eta_history(check_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
            """
        )
        # Additive migration for older databases that pre-date the environment column.
        # SQLite has no `ADD COLUMN IF NOT EXISTS`, so detect and add.
        cols = {row["name"] for row in cx.execute("PRAGMA table_info(runs)").fetchall()}
        if "environment" not in cols:
            cx.execute("ALTER TABLE runs ADD COLUMN environment TEXT NOT NULL DEFAULT 'production'")


@contextmanager
def _connect():
    cx = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA foreign_keys=ON")
    try:
        yield cx
    finally:
        cx.close()


# --- Runs ---

def create_run(url: str, environment: str = "production") -> str:
    run_id = uuid.uuid4().hex[:12]
    with _connect() as cx:
        cx.execute(
            "INSERT INTO runs(id, url, status, started_at, environment) VALUES (?, ?, 'queued', ?, ?)",
            (run_id, url, _now_ms(), environment),
        )
    return run_id


def set_run_status(run_id: str, status: str, error: str | None = None) -> None:
    with _connect() as cx:
        if status == "running":
            cx.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        else:
            cx.execute(
                "UPDATE runs SET status=?, completed_at=?, error=? WHERE id=?",
                (status, _now_ms(), error, run_id),
            )


def set_run_summary(run_id: str, summary: dict) -> None:
    with _connect() as cx:
        cx.execute("UPDATE runs SET summary_json=? WHERE id=?", (json.dumps(summary), run_id))


def get_run(run_id: str) -> dict | None:
    with _connect() as cx:
        row = cx.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d.pop("summary_json")) if d.get("summary_json") else None
        return d


def list_runs(limit: int = 20) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT id, url, status, started_at, completed_at FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Check results ---

def upsert_check_result(
    run_id: str,
    check_id: str,
    status: str,
    summary: str = "",
    details: dict | None = None,
    started_at: int | None = None,
    completed_at: int | None = None,
) -> None:
    with _connect() as cx:
        cx.execute(
            """
            INSERT INTO check_results(run_id, check_id, status, summary, details_json, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, check_id) DO UPDATE SET
              status=excluded.status,
              summary=excluded.summary,
              details_json=excluded.details_json,
              started_at=COALESCE(check_results.started_at, excluded.started_at),
              completed_at=excluded.completed_at
            """,
            (run_id, check_id, status, summary, json.dumps(details or {}), started_at, completed_at),
        )


def get_check_results(run_id: str) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT * FROM check_results WHERE run_id=? ORDER BY check_id", (run_id,)
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d.pop("details_json")) if d.get("details_json") else {}
            out.append(d)
        return out


# --- Manual toggles ---

def set_manual_toggle(run_id: str, check_id: str, marked_status: str) -> None:
    with _connect() as cx:
        cx.execute(
            """
            INSERT INTO manual_toggles(run_id, check_id, marked_status, marked_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id, check_id) DO UPDATE SET
              marked_status=excluded.marked_status,
              marked_at=excluded.marked_at
            """,
            (run_id, check_id, marked_status, _now_ms()),
        )


def clear_manual_toggle(run_id: str, check_id: str) -> None:
    with _connect() as cx:
        cx.execute("DELETE FROM manual_toggles WHERE run_id=? AND check_id=?", (run_id, check_id))


def get_manual_toggles(run_id: str) -> dict[str, str]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT check_id, marked_status FROM manual_toggles WHERE run_id=?", (run_id,)
        ).fetchall()
        return {r["check_id"]: r["marked_status"] for r in rows}


# --- ETA ---

def record_runtime(check_id: str, runtime_ms: int) -> None:
    with _connect() as cx:
        cx.execute(
            "INSERT INTO eta_history(check_id, runtime_ms, recorded_at) VALUES (?, ?, ?)",
            (check_id, runtime_ms, _now_ms()),
        )
        # Trim to last 20 per check.
        cx.execute(
            """
            DELETE FROM eta_history
            WHERE check_id=? AND recorded_at NOT IN (
              SELECT recorded_at FROM eta_history WHERE check_id=? ORDER BY recorded_at DESC LIMIT 20
            )
            """,
            (check_id, check_id),
        )


def get_runtime_averages() -> dict[str, float]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT check_id, AVG(runtime_ms) AS avg_ms FROM eta_history GROUP BY check_id"
        ).fetchall()
        return {r["check_id"]: float(r["avg_ms"]) for r in rows}
