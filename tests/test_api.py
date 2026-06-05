from __future__ import annotations

import asyncio
import sys
from typing import Any, cast

import httpx
import pytest


async def wait_for_process(client: httpx.AsyncClient, pid: int, status: str, timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/processes/{pid}")
        if response.status_code == 200 and response.json()["status"] == status:
            return response.json()
        await asyncio.sleep(0.05)
    raise AssertionError(f"process {pid} did not reach {status}")


@pytest.mark.asyncio
async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_run_command(client: httpx.AsyncClient) -> None:
    response = await client.post("/run", json={"command": [sys.executable, "-c", "print('hello')"]})
    assert response.status_code == 200
    assert response.json()["pid"] > 0
    assert response.json()["output_file"]


@pytest.mark.asyncio
async def test_run_command_with_label(client: httpx.AsyncClient) -> None:
    response = await client.post("/run", json={"command": [sys.executable, "-c", "print('hello')"], "label": "demo"})
    pid = response.json()["pid"]
    process = await wait_for_process(client, pid, "exited")
    assert process["label"] == "demo"


@pytest.mark.asyncio
async def test_run_command_with_timeout(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/run",
        json={"command": [sys.executable, "-c", "import time; time.sleep(5)"], "timeout": 0.2},
    )
    pid = response.json()["pid"]
    process = await wait_for_process(client, pid, "killed")
    assert process["status"] == "killed"


@pytest.mark.asyncio
async def test_list_processes(client: httpx.AsyncClient) -> None:
    await client.post("/run", json={"command": [sys.executable, "-c", "print('one')"]})
    response = await client.get("/processes")
    assert response.status_code == 200
    assert len(response.json()) >= 1


@pytest.mark.asyncio
async def test_list_processes_running_only(client: httpx.AsyncClient) -> None:
    await client.post("/run", json={"command": [sys.executable, "-c", "import time; time.sleep(1)"]})
    await client.post("/run", json={"command": [sys.executable, "-c", "print('done')"]})
    await asyncio.sleep(0.1)
    response = await client.get("/processes", params={"running_only": True})
    assert all(item["status"] == "running" for item in response.json())


@pytest.mark.asyncio
async def test_get_process_detail(client: httpx.AsyncClient) -> None:
    response = await client.post("/run", json={"command": [sys.executable, "-c", "print('hi')"]})
    pid = response.json()["pid"]
    detail = await client.get(f"/processes/{pid}")
    assert detail.status_code == 200
    assert detail.json()["pid"] == pid


@pytest.mark.asyncio
async def test_get_process_not_found(client: httpx.AsyncClient) -> None:
    response = await client.get("/processes/999999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_stop_process(client: httpx.AsyncClient) -> None:
    response = await client.post("/run", json={"command": [sys.executable, "-c", "import time; time.sleep(5)"]})
    pid = response.json()["pid"]
    stopped = await client.post(f"/processes/{pid}/stop")
    assert stopped.status_code == 200
    process = await wait_for_process(client, pid, "killed")
    assert process["status"] == "killed"


@pytest.mark.asyncio
async def test_get_output(client: httpx.AsyncClient) -> None:
    response = await client.post("/run", json={"command": [sys.executable, "-c", "print('alpha')"]})
    pid = response.json()["pid"]
    await wait_for_process(client, pid, "exited")
    output = await client.get(f"/processes/{pid}/output")
    assert "alpha" in output.json()["content"]


@pytest.mark.asyncio
async def test_get_output_tail(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/run",
        json={"command": [sys.executable, "-c", "print('1'); print('2'); print('3')"]},
    )
    pid = response.json()["pid"]
    await wait_for_process(client, pid, "exited")
    output = await client.get(f"/processes/{pid}/output", params={"tail": 2})
    assert output.json()["content"] == "2\n3"


@pytest.mark.asyncio
async def test_api_based_always_on_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/run",
        json={"command": [sys.executable, "-c", "import time; time.sleep(5)"], "label": "demo", "always_on": True},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_services_endpoint_not_available(client: httpx.AsyncClient) -> None:
    response = await client.get("/services")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_restart_unknown_declared_service_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/declared-services/missing/restart")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_openapi_spec(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert "paths" in response.json()


@pytest.mark.asyncio
async def test_list_scheduled_empty(client: httpx.AsyncClient) -> None:
    response = await client.get("/scheduled")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_scheduled_job_appears_while_pending(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/run",
        json={"command": [sys.executable, "-c", "print('delayed')"], "label": "delayed_test", "delay_seconds": 5},
    )
    assert response.status_code == 200
    assert response.json()["pid"] == 0

    scheduled = await client.get("/scheduled")
    assert scheduled.status_code == 200
    jobs = scheduled.json()
    assert len(jobs) == 1
    assert jobs[0]["label"] == "delayed_test"
    assert jobs[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_cancel_scheduled_job(client: httpx.AsyncClient) -> None:
    await client.post(
        "/run",
        json={"command": [sys.executable, "-c", "print('never')"], "label": "cancel_me", "delay_seconds": 60},
    )

    scheduled = await client.get("/scheduled")
    job_id = scheduled.json()[0]["id"]

    cancel = await client.post(f"/scheduled/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"

    verify = await client.get("/scheduled")
    assert verify.json()[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_completed_scheduled_job_returns_409(client: httpx.AsyncClient) -> None:
    await client.post(
        "/run",
        json={"command": [sys.executable, "-c", "print('fast')"], "label": "fast_delay", "delay_seconds": 0.1},
    )
    await asyncio.sleep(1.0)

    scheduled = await client.get("/scheduled")
    jobs = [j for j in scheduled.json() if j["label"] == "fast_delay"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"

    cancel = await client.post(f"/scheduled/{jobs[0]['id']}/cancel")
    assert cancel.status_code == 409


@pytest.mark.asyncio
async def test_cancel_nonexistent_scheduled_job(client: httpx.AsyncClient) -> None:
    response = await client.post("/scheduled/nonexistent_id/cancel")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_scheduled_job_completes_after_delay(client: httpx.AsyncClient) -> None:
    await client.post(
        "/run",
        json={"command": [sys.executable, "-c", "print('done')"], "label": "completes", "delay_seconds": 0.2},
    )
    await asyncio.sleep(1.0)

    scheduled = await client.get("/scheduled")
    jobs = [j for j in scheduled.json() if j["label"] == "completes"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["result_pid"] is not None and jobs[0]["result_pid"] > 0
