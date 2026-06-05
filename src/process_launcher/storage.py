from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import MisfirePolicy, ScheduledJob, ScheduledStatus


SCHEMA_VERSION = 1


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            row["version"]
            for row in self._conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        if 1 not in applied:
            self._conn.execute(
                """
                CREATE TABLE scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    label TEXT,
                    command_json TEXT NOT NULL,
                    cwd TEXT,
                    env_json TEXT NOT NULL,
                    timeout REAL,
                    scheduled_at TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    misfire_policy TEXT NOT NULL,
                    result_pid INTEGER,
                    last_error TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX idx_scheduled_jobs_status_run_at ON scheduled_jobs(status, run_at)")
            self._conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (1, "create_scheduled_jobs", _serialize_datetime(datetime.now())),
            )
            self._conn.commit()

    def upsert_scheduled_job(self, job: ScheduledJob) -> None:
        now = _serialize_datetime(datetime.now())
        self._conn.execute(
            """
            INSERT INTO scheduled_jobs(
                id, label, command_json, cwd, env_json, timeout, scheduled_at, run_at,
                status, misfire_policy, result_pid, last_error, started_at, completed_at,
                cancelled_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label = excluded.label,
                command_json = excluded.command_json,
                cwd = excluded.cwd,
                env_json = excluded.env_json,
                timeout = excluded.timeout,
                scheduled_at = excluded.scheduled_at,
                run_at = excluded.run_at,
                status = excluded.status,
                misfire_policy = excluded.misfire_policy,
                result_pid = excluded.result_pid,
                last_error = excluded.last_error,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                cancelled_at = excluded.cancelled_at,
                updated_at = excluded.updated_at
            """,
            _job_values(job, created_at=now, updated_at=now),
        )
        self._conn.commit()

    def update_scheduled_job(self, job: ScheduledJob) -> None:
        self.upsert_scheduled_job(job)

    def list_scheduled_jobs(self) -> list[ScheduledJob]:
        rows = self._conn.execute("SELECT * FROM scheduled_jobs ORDER BY scheduled_at").fetchall()
        return [_row_to_job(row) for row in rows]

    def list_pending_scheduled_jobs(self) -> list[ScheduledJob]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_jobs WHERE status = ? ORDER BY run_at",
            (ScheduledStatus.PENDING.value,),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def mark_stale_running_jobs_failed(self) -> None:
        now = _serialize_datetime(datetime.now())
        self._conn.execute(
            """
            UPDATE scheduled_jobs
            SET status = ?, last_error = ?, completed_at = ?, updated_at = ?
            WHERE status = ?
            """,
            (
                ScheduledStatus.FAILED.value,
                "launcher restarted while scheduled job was running",
                now,
                now,
                ScheduledStatus.RUNNING.value,
            ),
        )
        self._conn.commit()


def _job_values(job: ScheduledJob, *, created_at: str, updated_at: str) -> tuple[Any, ...]:
    return (
        job.id,
        job.label,
        json.dumps(job.command),
        job.cwd,
        json.dumps(job.env),
        job.timeout,
        _serialize_datetime(job.scheduled_at),
        _serialize_datetime(job.run_at),
        job.status.value,
        job.misfire_policy.value,
        job.result_pid,
        job.last_error,
        _serialize_optional_datetime(job.started_at),
        _serialize_optional_datetime(job.completed_at),
        _serialize_optional_datetime(job.cancelled_at),
        created_at,
        updated_at,
    )


def _row_to_job(row: sqlite3.Row) -> ScheduledJob:
    return ScheduledJob(
        id=row["id"],
        label=row["label"],
        command=json.loads(row["command_json"]),
        cwd=row["cwd"],
        env=json.loads(row["env_json"]),
        timeout=row["timeout"],
        scheduled_at=_parse_datetime(row["scheduled_at"]),
        run_at=_parse_datetime(row["run_at"]),
        status=ScheduledStatus(row["status"]),
        misfire_policy=MisfirePolicy(row["misfire_policy"]),
        result_pid=row["result_pid"],
        last_error=row["last_error"],
        started_at=_parse_optional_datetime(row["started_at"]),
        completed_at=_parse_optional_datetime(row["completed_at"]),
        cancelled_at=_parse_optional_datetime(row["cancelled_at"]),
    )


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()


def _serialize_optional_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
