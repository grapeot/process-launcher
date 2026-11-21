# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from threading import Event
from typing import cast

import httpx
import pytest

from process_launcher.log import HeartbeatLogger, OutputLogger, utc_now
from process_launcher.models import LauncherConfig, LoggingConfig, ProcessStatus, RunRequest, ServiceConfig, StorageConfig
from process_launcher.process import ProcessManager
from process_launcher.retention import LogRetention
from process_launcher.service_monitor import ServiceMonitor
from process_launcher.server import create_app


async def wait_for_exit(manager: ProcessManager, pid: int, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        process = manager.get_process(pid)
        if process and process.status != ProcessStatus.RUNNING:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("process did not exit")


@pytest.mark.asyncio
async def test_heartbeat_written_on_start(tmp_path: Path) -> None:
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    output = OutputLogger(tmp_path / "logs")
    manager = ProcessManager(heartbeat, output)
    response = await manager.start_process(RunRequest(command=[sys.executable, "-c", "import time; time.sleep(0.2)"], label="demo"))
    events = heartbeat.read_events(limit=10)
    assert any(event["event"] == "PROCESS_STARTED" and event["pid"] == response.pid for event in events)
    await manager.stop_all()


@pytest.mark.asyncio
async def test_heartbeat_written_on_exit(tmp_path: Path) -> None:
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    output = OutputLogger(tmp_path / "logs")
    manager = ProcessManager(heartbeat, output)
    response = await manager.start_process(RunRequest(command=[sys.executable, "-c", "print('done')"], label="demo"))
    await wait_for_exit(manager, response.pid)
    events = heartbeat.read_events(limit=10, event="PROCESS_EXITED")
    assert any(event["pid"] == response.pid and event["exit_code"] == 0 for event in events)


@pytest.mark.asyncio
async def test_heartbeat_written_on_circuit_breaker(tmp_path: Path) -> None:
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    output = OutputLogger(tmp_path / "logs")
    manager = ProcessManager(heartbeat, output)
    monitor = ServiceMonitor(manager, heartbeat)
    monitor.register_services(
        {
            "demo": ServiceConfig(
                label="demo",
                command=[sys.executable, "-c", "import sys; sys.exit(1)"],
                restart_delay=0.05,
                max_restarts=1,
            )
        }
    )
    await monitor.start_registered_services()
    await asyncio.sleep(0.2)
    events = heartbeat.read_events(limit=20, event="CIRCUIT_BREAKER")
    assert any(event["label"] == "demo" for event in events)
    await monitor.shutdown()


def test_heartbeat_daily_rotation(tmp_path: Path) -> None:
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    heartbeat.write_event("PROCESS_STARTED", pid=1)
    files = list((tmp_path / "logs").glob("heartbeat_*.jsonl"))
    assert len(files) == 1


def test_heartbeat_cleanup_old_files(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old_file = log_dir / "heartbeat_2000-01-01.jsonl"
    old_file.write_text("{}\n", encoding="utf-8")
    old_time = time.time() - 40 * 24 * 3600
    os.utime(old_file, (old_time, old_time))
    heartbeat = HeartbeatLogger(log_dir, retention_days=30)
    today = log_dir / f"heartbeat_{utc_now().date().isoformat()}.jsonl"
    today.write_text("{}\n", encoding="utf-8")
    os.utime(today, (old_time, old_time))
    heartbeat.write_event("TEST")
    assert old_file.exists()
    os.utime(today, (old_time, old_time))
    assert heartbeat.cleanup() == 1
    assert not old_file.exists()
    assert today.exists()


@pytest.mark.asyncio
async def test_output_file_created(tmp_path: Path) -> None:
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    output = OutputLogger(tmp_path / "logs")
    manager = ProcessManager(heartbeat, output)
    response = await manager.start_process(RunRequest(command=[sys.executable, "-c", "print('hello')"], label="demo"))
    await wait_for_exit(manager, response.pid)
    output_file = cast(str, response.output_file)
    assert Path(output_file).exists()


@pytest.mark.asyncio
async def test_output_file_content(tmp_path: Path) -> None:
    heartbeat = HeartbeatLogger(tmp_path / "logs")
    output = OutputLogger(tmp_path / "logs")
    manager = ProcessManager(heartbeat, output)
    response = await manager.start_process(
        RunRequest(command=[sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err\\n')"], label="demo")
    )
    await wait_for_exit(manager, response.pid)
    output_file = cast(str, response.output_file)
    content = Path(output_file).read_text(encoding="utf-8")
    assert "out" in content
    assert "err" in content


def test_output_cleanup_old_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "logs" / "output"
    output_dir.mkdir(parents=True)
    old_file = output_dir / "old.log"
    old_file.write_text("old", encoding="utf-8")
    old_time = time.time() - 40 * 24 * 3600
    os.utime(old_file, (old_time, old_time))
    output = OutputLogger(tmp_path / "logs", retention_days=30)
    protected = output.create_output_file("active", utc_now())
    os.utime(protected, (old_time, old_time))
    assert old_file.exists()
    assert output.cleanup(protected=lambda: {protected}) == 1
    assert not old_file.exists()
    assert protected.exists()


@pytest.mark.asyncio
async def test_slow_retention_does_not_block_api_or_process_start(tmp_path: Path, monkeypatch) -> None:
    """Hold the next startup sweep open while both API paths complete."""
    entered, release, timed_out = Event(), Event(), Event()
    output = OutputLogger(tmp_path / "logs")
    old = output.output_dir / "old.log"
    old.touch()
    old_time = time.time() - 40 * 24 * 3600
    os.utime(old, (old_time, old_time))
    original_lstat = Path.lstat

    def slow_lstat(path):
        if path == old:
            entered.set()
            if not release.wait(timeout=5):
                timed_out.set()
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", slow_lstat)
    app = create_app(config=LauncherConfig(logging=LoggingConfig(dir=str(tmp_path / "logs")),
                                           storage=StorageConfig(sqlite_path=str(tmp_path / "launcher.db"))))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                assert await asyncio.to_thread(entered.wait, 2)
                assert (await api.get("/logs/retention")).json()["running"] is True
                assert (await api.get("/health")).status_code == 200
                response = await api.post("/run", json={"command": [sys.executable, "-c", "print('ok')"]})
                assert response.status_code == 200
                assert not release.is_set()
                release.set()
    finally:
        release.set()
    assert not timed_out.is_set()


@pytest.mark.asyncio
async def test_retention_preserves_old_log_of_running_process(tmp_path: Path) -> None:
    heartbeat = HeartbeatLogger(tmp_path)
    output = OutputLogger(tmp_path)
    manager = ProcessManager(heartbeat, output)
    response = await manager.start_process(
        RunRequest(command=[sys.executable, "-c", "import time; time.sleep(60)"])
    )
    path = Path(cast(str, response.output_file))
    old_time = time.time() - 40 * 24 * 3600
    try:
        os.utime(path, (old_time, old_time))
        assert output.cleanup(protected=manager.active_output_paths) == 0
        assert path.exists()
    finally:
        await manager.stop_all()
    assert output.cleanup(protected=manager.active_output_paths) == 1
    assert not path.exists()


def test_retention_retries_after_failure(tmp_path: Path, monkeypatch, caplog) -> None:
    heartbeat = HeartbeatLogger(tmp_path)
    output = OutputLogger(tmp_path)
    original = output.cleanup
    attempts = 0
    allow_retry = Event()

    def flaky_cleanup(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary failure")
        allow_retry.wait(timeout=3)
        return original(**kwargs)

    def wait_for_status(predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = retention.status()
            if predicate(status):
                return status
            time.sleep(0.01)
        pytest.fail(f"status did not converge: {retention.status()}")

    monkeypatch.setattr(output, "cleanup", flaky_cleanup)
    retention = LogRetention(heartbeat, output, set, interval=0.01)
    retention.start()
    try:
        failed = wait_for_status(lambda status: status["last_error"] == "output: temporary failure")
        assert failed["last_success_at"] is None
        assert "output log retention sweep failed" in caplog.text
        allow_retry.set()
        recovered = wait_for_status(lambda status: status["last_success_at"] is not None)
        assert recovered["last_error"] is None
    finally:
        allow_retry.set()
        retention.stop()
