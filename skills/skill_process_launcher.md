# Process Launcher Skill

## Purpose

Use Process Launcher when a local workflow needs to start a command through a trusted localhost API, inspect process status, read output logs, or manage a small always-on service.

## Setup

Install the package in editable mode:

```bash
uv venv
uv pip install -e '.[dev]'
```

Create local config from the public example:

```bash
cp config/launcher.example.yaml config/launcher.yaml
```

Put real paths, service labels, and `.env` references only in the ignored local config or in a private overlay.

## Start The Launcher

```bash
process-launcher start --config config/launcher.yaml
```

The module entrypoint is equivalent:

```bash
python -m process_launcher start --config config/launcher.yaml
```

## Common Calls

Health check:

```bash
curl -sf http://127.0.0.1:7997/health
```

Run a command:

```bash
curl -sf -X POST http://127.0.0.1:7997/run \
  -H 'Content-Type: application/json' \
  -d '{"command": ["python", "-c", "print(\"hello\")"], "label": "demo"}'
```

List processes and read output:

```bash
curl -sf http://127.0.0.1:7997/processes
curl -sf http://127.0.0.1:7997/processes/{pid}/output
```

Schedule and cancel an in-memory delayed launch:

```bash
curl -sf -X POST http://127.0.0.1:7997/run \
  -H 'Content-Type: application/json' \
  -d '{"command": ["python", "-c", "print(\"later\")"], "label": "later", "delay_seconds": 300}'

curl -sf http://127.0.0.1:7997/scheduled
curl -sf -X POST http://127.0.0.1:7997/scheduled/{job_id}/cancel
```

Inspect services and logs:

```bash
curl -sf http://127.0.0.1:7997/services
curl -sf http://127.0.0.1:7997/logs/heartbeat
curl -sf http://127.0.0.1:7997/logs/output
```

## Private Overlay Pattern

This skill is intentionally generic. Keep user-specific aliases, real job recipes, private domains, personal paths, and notification policies in a separate private skill or workspace file. The private overlay can call the public API examples here without modifying this repository.

## Safety Notes

`POST /run` executes commands as the launcher user. Use this service only on trusted localhost interfaces unless you add an authentication and authorization layer.
