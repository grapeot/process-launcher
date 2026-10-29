from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

from process_launcher.config import load_config


async def wait_for(check):
    async with asyncio.timeout(10):
        while not (result := check()):
            await asyncio.sleep(0.02)
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, True, False])
async def test_service_policy_survives_start_restarts_and_real_failures(
    client, app, tmp_path: Path, policy: bool | None,
):
    declaration = {
        "label": "example_service",
        "command": [sys.executable, "-c", "print('example service output'); raise SystemExit(7)"],
        "restart_delay": 0.02,
        "max_restarts": 2,
        "restart_window": 60,
    }
    if policy is not None:
        declaration["diagnose_on_failure"] = policy
    config_path = tmp_path / "services.yaml"
    config_path.write_text(yaml.safe_dump({"services": {"example_service": declaration}}))
    monitor = app.state.service_monitor
    monitor.register_services(load_config(config_path).services)
    await monitor.start_registered_services()
    expected = policy is not False

    await wait_for(lambda: any(
        item["label"] == "example_service" and item["status"] == "circuit_breaker"
        for item in monitor.list_services()
    ))
    processes = (await client.get("/processes")).json()
    attempts = [process for process in processes if process["label"] == "example_service"]
    assert len(attempts) == 2
    assert {process["restart_count"] for process in attempts} == {0, 1}
    for process in attempts:
        assert process["diagnose_on_failure"] is expected
        assert process["exit_code"] == 7 and process["status"] == "exited"
        assert "example service output" in Path(process["output_file"]).read_text()

    restarted = await client.post("/declared-services/example_service/restart")
    assert restarted.status_code == 200
    info = restarted.json()
    assert info["diagnose_on_failure"] is expected
    assert info["restart_count"] == 0
    await wait_for(lambda: app.state.process_manager.get_process(info["pid"]).exit_code == 7)
    await wait_for(lambda: any(
        event.get("pid") == info["pid"]
        for event in app.state.heartbeat_logger.read_events(
            event="PROCESS_EXITED", label="example_service")
    ))
    readback = (await client.get(f"/processes/{info['pid']}")).json()
    assert readback["diagnose_on_failure"] is expected
    assert readback["exit_code"] == 7

    events = app.state.heartbeat_logger.read_events(label="example_service")
    lifecycle = [event for event in events if event["event"] in ("PROCESS_STARTED", "PROCESS_EXITED")]
    assert len(lifecycle) >= 6
    assert all(event["diagnose_on_failure"] is expected for event in lifecycle)
    assert all(event["exit_code"] == 7 for event in lifecycle if event["event"] == "PROCESS_EXITED")
    service_events = [event for event in events if event["event"] in ("RESTART", "CIRCUIT_BREAKER")]
    assert {event["event"] for event in service_events} == {"RESTART", "CIRCUIT_BREAKER"}
    assert all(event["diagnose_on_failure"] is expected for event in service_events)

    await wait_for(lambda: any(
        item["label"] == "example_service" and item["status"] == "circuit_breaker"
        for item in monitor.list_services()
    ))
    reset = await monitor.reset_circuit_breaker("example_service")
    assert reset.diagnose_on_failure is expected
    resets = app.state.heartbeat_logger.read_events(
        event="CIRCUIT_BREAKER_RESET", label="example_service")
    assert resets[-1]["diagnose_on_failure"] is expected


@pytest.mark.asyncio
async def test_service_manual_stop_keeps_optout(client, app):
    from process_launcher.models import ServiceConfig

    monitor = app.state.service_monitor
    monitor.register_services({"example_service": ServiceConfig(
        label="example_service", command=[sys.executable, "-c", "import time; time.sleep(60)"],
        diagnose_on_failure=False,
    )})
    await monitor.start_registered_services()
    process = next(item for item in (await client.get("/processes")).json()
                   if item["label"] == "example_service")
    stopped = await client.post(f"/processes/{process['pid']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "killed"
    assert stopped.json()["diagnose_on_failure"] is False
    events = await wait_for(lambda: app.state.heartbeat_logger.read_events(
        event="PROCESS_EXITED", label="example_service"))
    assert events[-1]["diagnose_on_failure"] is False
    assert events[-1]["status"] == "killed"
