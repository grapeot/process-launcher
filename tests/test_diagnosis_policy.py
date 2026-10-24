from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

from process_launcher.config import load_config
from process_launcher.log import HeartbeatLogger, OutputLogger
from process_launcher.models import PeriodicRun, ScheduledJob
from process_launcher.periodic import PeriodicManager
from process_launcher.process import ProcessManager
from process_launcher.scheduled import ScheduledManager
from process_launcher.storage import SQLiteStore


async def wait_for(check):
    async with asyncio.timeout(10):
        while not (result := check()):
            await asyncio.sleep(0.02)
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, True, False])
async def test_immediate_failure_preserves_policy_and_failure(client, app, policy):
    payload = {"command": [sys.executable, "-c", "raise SystemExit(7)"]}
    if policy is not None:
        payload["diagnose_on_failure"] = policy
    response = await client.post("/run", json=payload)
    assert response.status_code == 200
    expected = policy is not False
    assert response.json()["diagnose_on_failure"] is expected
    pid = response.json()["pid"]
    events = await wait_for(lambda: app.state.heartbeat_logger.read_events(event="PROCESS_EXITED"))
    info = (await client.get(f"/processes/{pid}")).json()
    assert info["diagnose_on_failure"] is expected
    assert info["exit_code"] == 7 and info["status"] == "exited"
    assert events[-1]["diagnose_on_failure"] is expected
    assert events[-1]["exit_code"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_optout_survives_timeout_and_manual_stop(client, app, stop):
    response = await client.post("/run", json={
        "command": [sys.executable, "-c", "import time; time.sleep(60)"],
        "diagnose_on_failure": False, "timeout": 60 if stop else 0.1,
    })
    pid = response.json()["pid"]
    if stop:
        assert (await client.post(f"/processes/{pid}/stop")).status_code == 200
    events = await wait_for(lambda: app.state.heartbeat_logger.read_events(event="PROCESS_EXITED"))
    assert events[-1]["diagnose_on_failure"] is False
    assert events[-1]["status"] == "killed"


@pytest.mark.asyncio
async def test_pending_policy_can_be_patched_without_rescheduling_time(client):
    response = await client.post("/run", json={
        "command": ["true"], "delay_seconds": 3600, "diagnose_on_failure": False,
    })
    assert response.json()["diagnose_on_failure"] is False
    job_id = response.json()["scheduled_job_id"]
    before = next(j for j in (await client.get("/scheduled")).json() if j["id"] == job_id)
    for policy in (True, False):
        updated = await client.patch(f"/scheduled/{job_id}", json={"diagnose_on_failure": policy})
        assert updated.status_code == 200
        assert updated.json()["diagnose_on_failure"] is policy
        assert updated.json()["run_at"] == before["run_at"]


def test_migrate_v2_defaults_existing_rows_to_enabled(tmp_path):
    path = tmp_path / "old.db"
    store = SQLiteStore(path)
    job = ScheduledJob(command=["true"], scheduled_at=datetime.now(), run_at=datetime.now())
    run = PeriodicRun(label="example", command=["true"], scheduled_for=datetime.now())
    store.upsert_scheduled_job(job)
    store.upsert_periodic_run(run)
    store.close()
    # Reconstruct the v2 layout, including historical rows, in this disposable DB.
    with sqlite3.connect(path) as legacy:
        for table in ("scheduled_jobs", "periodic_runs"):
            legacy.execute(f"ALTER TABLE {table} DROP COLUMN diagnose_on_failure")
        legacy.execute("DELETE FROM schema_migrations WHERE version=3")
    migrated = SQLiteStore(path)
    try:
        assert migrated.list_scheduled_jobs()[0].diagnose_on_failure is True
        assert migrated.list_periodic_runs()[0].diagnose_on_failure is True
        assert migrated.list_scheduled_jobs()[0].id == job.id
        assert migrated.list_periodic_runs()[0].id == run.id
    finally:
        migrated.close()


@pytest.mark.asyncio
async def test_delayed_optout_survives_recovery_and_nonzero_exit(tmp_path):
    path = tmp_path / "launcher.db"
    original = SQLiteStore(path)
    job = ScheduledJob(command=[sys.executable, "-c", "raise SystemExit(9)"],
                       scheduled_at=datetime.now(), run_at=datetime.now()-timedelta(seconds=1),
                       diagnose_on_failure=False)
    original.upsert_scheduled_job(job)
    original.close()
    store = SQLiteStore(path)
    manager = ScheduledManager(store)
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    processes = ProcessManager(heartbeat, OutputLogger(tmp_path / "logs"))
    try:
        manager.recover_pending(processes)
        await wait_for(lambda: manager.list_jobs()[0].status == "failed")
        restored = manager.list_jobs()[0]
        assert restored.diagnose_on_failure is False
        assert heartbeat.read_events(event="PROCESS_EXITED")[-1]["diagnose_on_failure"] is False
        assert processes.get_process(restored.result_pid).exit_code == 9
    finally:
        manager.shutdown()
        await processes.stop_all()
        store.close()


def test_migration_failure_rolls_back_both_tables_and_can_retry(tmp_path):
    store = SQLiteStore(tmp_path / "interrupted.db")
    job = ScheduledJob(command=["true"], scheduled_at=datetime.now(), run_at=datetime.now())
    run = PeriodicRun(label="example", command=["true"], scheduled_for=datetime.now())
    store.upsert_scheduled_job(job)
    store.upsert_periodic_run(run)
    with store._conn:
        for table in ("scheduled_jobs", "periodic_runs"):
            store._conn.execute(f"ALTER TABLE {table} DROP COLUMN diagnose_on_failure")
        store._conn.execute("DELETE FROM schema_migrations WHERE version=3")

    def fail_second_alter(action, first, second, database, trigger):
        return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE and second == "periodic_runs"
                else sqlite3.SQLITE_OK)

    try:
        store._conn.set_authorizer(fail_second_alter)
        with pytest.raises(sqlite3.DatabaseError):
            store.migrate()
        store._conn.set_authorizer(None)
        for table in ("scheduled_jobs", "periodic_runs"):
            assert "diagnose_on_failure" not in {r["name"] for r in store._conn.execute(f"PRAGMA table_info({table})")}
        assert store._conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone() is None
        store.migrate()
        assert store.list_scheduled_jobs()[0].id == job.id
        assert store.list_periodic_runs()[0].id == run.id
        assert store.list_scheduled_jobs()[0].diagnose_on_failure is True
        assert store.list_periodic_runs()[0].diagnose_on_failure is True
    finally:
        store._conn.set_authorizer(None)
        store.close()


@pytest.mark.asyncio
async def test_periodic_policy_is_snapshot_even_after_reload(tmp_path):
    store = SQLiteStore(tmp_path / "periodic.db")
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    processes = ProcessManager(heartbeat, OutputLogger(tmp_path / "logs"))
    config_path = tmp_path / "launcher.yaml"
    config_path.write_text(
        f"periodic_jobs:\n  experiment:\n    label: experiment\n    command: [{sys.executable!r}, '-c', 'raise SystemExit(8)']\n"
        "    diagnose_on_failure: false\n    enabled: false\n"
        "    schedule: {type: interval, every_seconds: 3600}\n"
    )
    job = load_config(config_path).periodic_jobs["experiment"]
    manager = PeriodicManager(store, {job.label:job})
    try:
        await manager._start_run(processes, job, datetime.now())
        manager.reload_jobs(processes, {job.label:job.model_copy(update={"diagnose_on_failure":True})})
        await wait_for(lambda: store.list_periodic_runs()[0].status == "failed")
        stored = store.list_periodic_runs()[0]
        assert stored.diagnose_on_failure is False
        assert heartbeat.read_events(event="PROCESS_EXITED")[-1]["diagnose_on_failure"] is False
        assert processes.get_process(stored.result_pid).exit_code == 8
    finally:
        manager.shutdown()
        await processes.stop_all()
        store.close()


@pytest.mark.asyncio
async def test_scheduled_spawn_failure_keeps_policy(client, app, tmp_path):
    response = await client.post("/run", json={"command":[str(tmp_path/"missing")],
                                              "delay_seconds":0.01,"diagnose_on_failure":False})
    job_id = response.json()["scheduled_job_id"]
    await wait_for(lambda: any(j.id == job_id and j.status == "failed"
                              for j in app.state.scheduled_manager.list_jobs()))
    job = next(j for j in app.state.scheduled_manager.list_jobs() if j.id == job_id)
    assert job.diagnose_on_failure is False and job.result_pid == 0
