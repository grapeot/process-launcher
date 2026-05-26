# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml

from process_launcher.config import load_config
from fastapi import FastAPI

from process_launcher.server import create_app, initialize_app_state, shutdown_app_state


async def wait_for_status(client: httpx.AsyncClient, pid: int, status: str, timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/processes/{pid}")
        if response.status_code == 200 and response.json()["status"] == status:
            return response.json()
        await asyncio.sleep(0.05)
    raise AssertionError(f"process {pid} did not reach {status}")


@pytest.mark.asyncio
async def test_full_lifecycle(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "launcher.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "logging": {"dir": "logs", "heartbeat_retention_days": 30, "output_retention_days": 30},
                "services": {
                    "always": {
                        "label": "always",
                        "command": [sys.executable, "-c", "import time; time.sleep(5)"],
                        "restart_delay": 0.1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    app: FastAPI = create_app(config_path=config_path, config=load_config(config_path))
    await initialize_app_state(app, load_config(config_path), config_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            services = await client.get("/services")
            assert services.json()[0]["status"] == "running"
            run_response = await client.post("/run", json={"command": [sys.executable, "-c", "print('job')"], "label": "job"})
            pid = cast(int, run_response.json()["pid"])
            await wait_for_status(client, pid, "exited")
            stopped = await client.post(f"/services/{services.json()[0]['label']}/restart")
            assert stopped.status_code == 200
            heartbeat = await client.get("/logs/heartbeat", params={"label": "job"})
            assert len(heartbeat.json()) >= 2
    finally:
        await shutdown_app_state(app)


@pytest.mark.asyncio
async def test_crontab_simulation(client: httpx.AsyncClient) -> None:
    pids: list[int] = []
    for idx in range(3):
        response = await client.post(
            "/run",
            json={"command": [sys.executable, "-c", f"print('run-{idx}')"], "label": f"job-{idx}"},
        )
        pids.append(response.json()["pid"])
    for pid in pids:
        await wait_for_status(client, int(pid), "exited")
    heartbeat = await client.get("/logs/heartbeat", params={"event": "PROCESS_EXITED"})
    assert len(heartbeat.json()) >= 3


@pytest.mark.asyncio
async def test_launcher_restart_preserves_nothing(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "launcher.yaml"
    config_path.write_text(yaml.safe_dump({"logging": {"dir": "logs"}, "services": {}}), encoding="utf-8")

    app_one: FastAPI = create_app(config_path=config_path, config=load_config(config_path))
    await initialize_app_state(app_one, load_config(config_path), config_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_one), base_url="http://test") as client_one:
        response = await client_one.post("/run", json={"command": [sys.executable, "-c", "print('once')"]})
        pid = cast(int, response.json()["pid"])
        await wait_for_status(client_one, pid, "exited")
        assert len((await client_one.get("/processes")).json()) == 1
    await shutdown_app_state(app_one)

    app_two: FastAPI = create_app(config_path=config_path, config=load_config(config_path))
    await initialize_app_state(app_two, load_config(config_path), config_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_two), base_url="http://test") as client_two:
        assert (await client_two.get("/processes")).json() == []
    await shutdown_app_state(app_two)
