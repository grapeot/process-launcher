# Process Launcher

Process Launcher is a small localhost service that starts and tracks child processes through an HTTP API. It is designed for workflows where another tool, script, or AI agent needs a stable control surface for launching commands, checking status, reading captured output, and managing a few always-on local services.

The server binds to `127.0.0.1` by default. It accepts arbitrary commands from local callers, so it should be treated as a trusted local automation tool rather than a network-facing service.

## Features

- `POST /run` starts one-off commands and returns a PID plus output log path.
- `GET /processes` and `GET /processes/{pid}` report tracked process state.
- `GET /processes/{pid}/output` reads captured stdout and stderr.
- `delay_seconds` schedules an in-memory delayed launch that can be listed and cancelled.
- Always-on services can be configured with restart delay, restart window, and circuit breaker limits.
- Heartbeat and output logs are retained locally and ignored by git.

## Install

```bash
uv venv
uv pip install -e '.[dev]'
```

The package exposes both a console script and a module entrypoint:

```bash
process-launcher start --config config/launcher.yaml
python -m process_launcher start --config config/launcher.yaml
```

## Configure

Copy the public example and edit it for your machine:

```bash
cp config/launcher.example.yaml config/launcher.yaml
```

`config/launcher.yaml` is intentionally ignored by git. Keep real local paths, service names, tokens, and `.env` references there. The tracked repository only ships `config/launcher.example.yaml` with generic placeholder values.

## Run

```bash
process-launcher start --config config/launcher.yaml
```

Then call the API from another shell:

```bash
curl -sf http://127.0.0.1:7997/health
curl -sf -X POST http://127.0.0.1:7997/run \
  -H 'Content-Type: application/json' \
  -d '{"command": ["python", "-c", "print(\"hello\")"], "label": "hello"}'
```

## Private Overlay Pattern

This repository is public-ready and generic. It ships a reusable skill in `skills/skill_process_launcher.md`, public docs, and fake configuration examples. A user workspace can keep a private overlay separately: aliases, real job recipes, private paths, domain-specific service labels, and operational runbooks. That overlay should point to this package but should not be committed here.

## Testing

```bash
python -m pytest -v
python -m pytest -v -m live_integration
python -m pytest -v --cov=process_launcher --cov-report=term-missing
```

The default tests use temporary configs and short-lived subprocesses. The `live_integration` marker starts a real launcher process on port `7976` using the existing pytest fixture.
