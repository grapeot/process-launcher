# Test Plan

## Test Layers

The default test suite uses pytest, temporary config directories, and short-lived local subprocesses. It validates the package without requiring a pre-existing launcher process.

The `live_integration` marker is reserved for tests that start a real launcher server on port `7976` through the existing pytest fixture. These tests remain opt-in.

## Commands

```bash
python -m pytest -v
python -m pytest -v --cov=process_launcher --cov-report=term-missing
python -m pytest -v -m live_integration
```

## Coverage Areas

- Config parsing and `.env` loading.
- Process start, stop, timeout, cwd, env, output capture, and zombie reaping.
- Service monitor restart behavior and circuit breaker behavior.
- Heartbeat and output log writing, filtering, and retention cleanup.
- FastAPI endpoints for health, process control, scheduled jobs, services, logs, and OpenAPI.
- Live HTTP behavior through the `live_integration` fixture.

## Marker Semantics

`live_integration` tests may launch a real server process and submit commands through HTTP. The fixture checks whether port `7976` is free and skips if it is already occupied. The default test command should continue to avoid depending on a running external service.
