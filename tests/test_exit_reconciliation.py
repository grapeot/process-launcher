from __future__ import annotations

import asyncio
import sys
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from process_launcher.log import HeartbeatLogger, OutputLogger
from process_launcher.models import (
    PeriodicJobConfig, PeriodicRun, PeriodicRunStatus, PeriodicSchedule,
    ProcessInfo, ProcessStatus, RunRequest, RunResponse,
)
from process_launcher.periodic import PeriodicManager
from process_launcher.process import ProcessManager
from process_launcher.storage import SQLiteStore


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_exit_callback_runs_on_event_loop(tmp_path: Path, asynchronous: bool) -> None:
    manager = ProcessManager(HeartbeatLogger(tmp_path), OutputLogger(tmp_path))
    loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()
    delivered = loop.create_future()

    def callback(info: ProcessInfo) -> None:
        delivered.set_result((threading.get_ident(), asyncio.get_running_loop(), info.exit_code))

    async def async_callback(info: ProcessInfo) -> None:
        callback(info)

    try:
        await manager.start_process(
            RunRequest(command=[sys.executable, "-c", "pass"], timeout=5),
            on_exit=async_callback if asynchronous else callback,
        )
        assert await asyncio.wait_for(delivered, 5) == (owner_thread, loop, 0)
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_exit_callback_failure_is_observable(tmp_path: Path, asynchronous: bool) -> None:
    manager = ProcessManager(HeartbeatLogger(tmp_path), OutputLogger(tmp_path))
    loop = asyncio.get_running_loop()
    reported = loop.create_future()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.set_result(context))

    def callback(_info: ProcessInfo) -> None:
        raise ValueError("callback failed")

    async def async_callback(info: ProcessInfo) -> None:
        callback(info)

    try:
        await manager.start_process(
            RunRequest(command=[sys.executable, "-c", "pass"]),
            on_exit=async_callback if asynchronous else callback,
        )
        context = await asyncio.wait_for(reported, 5)
        assert context["message"] == "process exit callback failed"
        assert isinstance(context["exception"], ValueError)
    finally:
        await manager.stop_all()
        loop.set_exception_handler(old_handler)


@pytest.fixture
def periodic(tmp_path: Path):
    store = SQLiteStore(tmp_path / "state.sqlite")
    job = PeriodicJobConfig(label="sample", command=["true"], schedule=PeriodicSchedule(type="interval", every_seconds=60))
    manager = PeriodicManager(store, {job.label: job})
    try:
        yield manager, job, store
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [PeriodicRunStatus.COMPLETED, PeriodicRunStatus.FAILED])
async def test_fast_exit_before_start_returns_stays_terminal(periodic, terminal) -> None:
    manager, job, store = periodic

    async def start(_request, *, on_exit):
        info = ProcessInfo(pid=123, label=job.label, command="true", status=ProcessStatus.EXITED,
                           started_at=datetime.now(timezone.utc), exit_code=0 if terminal == PeriodicRunStatus.COMPLETED else 1)
        on_exit(info)
        return RunResponse(pid=123, label=job.label, started_at=info.started_at, output_file="result.log")

    process = Mock(start_process=start)
    await manager._start_run(process, job, datetime.now(timezone.utc))
    run = store.list_periodic_runs(job.label)[0]
    assert run.status == terminal
    assert run.result_pid == 123 and run.output_file == "result.log"
    assert run.completed_at is not None
    assert manager.get_job(job.label).runtime.active_pid is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state,code,expected", [
    (ProcessStatus.EXITED, 0, PeriodicRunStatus.COMPLETED),
    (ProcessStatus.EXITED, 2, PeriodicRunStatus.FAILED),
    (ProcessStatus.KILLED, 0, PeriodicRunStatus.FAILED),
])
async def test_missed_callback_reconciles_before_next_run(periodic, state, code, expected) -> None:
    manager, job, store = periodic
    old = PeriodicRun(label=job.label, command=job.command, scheduled_for=datetime.now(timezone.utc),
                      status=PeriodicRunStatus.RUNNING, result_pid=123, output_file="old.log")
    manager._update_run(old)
    info = ProcessInfo(pid=123, label=job.label, command="true", started_at=datetime.now(timezone.utc),
                       status=state, exit_code=code, output_file="old.log")
    process = Mock(get_process=Mock(return_value=info), start_process=AsyncMock(return_value=RunResponse(
        pid=124, label=job.label, started_at=datetime.now(timezone.utc), output_file="new.log")))
    await manager._start_run(process, job, datetime.now(timezone.utc))
    assert store.get_periodic_run(old.id).status == expected
    assert process.start_process.await_count == 1
    assert manager.get_job(job.label).runtime.active_pid == 124
    # A late old callback must not clear the new run's overlap guard.
    manager.mark_completed(old.id)
    assert manager.get_job(job.label).runtime.active_pid == 124


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["running", "missing", "reused_pid"])
async def test_does_not_release_unconfirmed_or_still_running_process(periodic, kind) -> None:
    manager, job, store = periodic
    old = PeriodicRun(label=job.label, command=job.command, scheduled_for=datetime.now(timezone.utc),
                      status=PeriodicRunStatus.RUNNING, result_pid=123, output_file="old.log")
    manager._update_run(old)
    info = None if kind == "missing" else ProcessInfo(
        pid=123, label=job.label, command="true", started_at=datetime.now(timezone.utc),
        status=ProcessStatus.RUNNING if kind == "running" else ProcessStatus.EXITED,
        exit_code=0, output_file="different.log" if kind == "reused_pid" else "old.log")
    process = Mock(get_process=Mock(return_value=info), start_process=AsyncMock())
    await manager._start_run(process, job, datetime.now(timezone.utc))
    assert process.start_process.await_count == 0
    assert store.get_periodic_run(old.id).status == PeriodicRunStatus.RUNNING
    assert store.list_periodic_runs(job.label)[-1].status == PeriodicRunStatus.SKIPPED


@pytest.mark.asyncio
async def test_initial_storage_failure_does_not_leave_running_placeholder(periodic, monkeypatch) -> None:
    manager, job, store = periodic
    original = store.upsert_periodic_run
    failures = [True]

    def fail_once(run):
        if failures:
            failures.pop()
            raise sqlite3.OperationalError("database is locked")
        original(run)

    monkeypatch.setattr(store, "upsert_periodic_run", fail_once)
    process = Mock(start_process=AsyncMock(return_value=RunResponse(
        pid=123, label=job.label, started_at=datetime.now(timezone.utc), output_file="result.log")))
    with pytest.raises(sqlite3.OperationalError):
        await manager._start_run(process, job, datetime.now(timezone.utc))
    assert job.label not in manager._active_runs
    await manager._start_run(process, job, datetime.now(timezone.utc))
    assert process.start_process.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_metadata_storage_failure_keeps_live_process_guard(periodic, monkeypatch, operation) -> None:
    manager, job, store = periodic
    original_read = store.get_periodic_run
    original_write = store.upsert_periodic_run
    calls = 0

    def fail_metadata_once(run):
        nonlocal calls
        calls += 1
        # Initial INSERT succeeds; metadata persistence fails after spawning.
        if calls == 2:
            raise sqlite3.OperationalError("database is locked")
        return original_write(run)

    def fail_read_once(run_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_read(run_id)

    monkeypatch.setattr(store, "get_periodic_run" if operation == "read" else "upsert_periodic_run",
                        fail_read_once if operation == "read" else fail_metadata_once)
    info = ProcessInfo(pid=123, label=job.label, command="true", started_at=datetime.now(timezone.utc),
                       status=ProcessStatus.RUNNING, output_file="result.log")
    process = Mock(get_process=Mock(return_value=info), start_process=AsyncMock(return_value=RunResponse(
        pid=123, label=job.label, started_at=info.started_at, output_file="result.log")))
    with pytest.raises(sqlite3.OperationalError):
        await manager._start_run(process, job, datetime.now(timezone.utc))
    assert manager.get_job(job.label).runtime.active_pid == 123
    await manager._start_run(process, job, datetime.now(timezone.utc))
    assert process.start_process.await_count == 1
    info.status = ProcessStatus.EXITED
    info.exit_code = 0
    await manager._start_run(process, job, datetime.now(timezone.utc))
    assert process.start_process.await_count == 2


@pytest.mark.asyncio
async def test_run_loop_retries_after_transient_start_error(periodic, monkeypatch) -> None:
    manager, job, _store = periodic
    job.schedule.every_seconds = 0.01
    retried = asyncio.Event()
    errors = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    calls = 0

    async def start(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        retried.set()

    monkeypatch.setattr(manager, "_start_run", start)
    task = asyncio.create_task(manager._run_loop(Mock(), job))
    try:
        await asyncio.wait_for(retried.wait(), 2)
        assert calls >= 2
        assert len(errors) == 1
        assert isinstance(errors[0]["exception"], sqlite3.OperationalError)
    finally:
        task.cancel()
        await task
        loop.set_exception_handler(old_handler)
