# Process Launcher Skill

## Purpose

Use Process Launcher when a local workflow needs to start a command through a trusted localhost API, inspect process status, read output logs, schedule durable one-shot jobs, or run a small set of YAML-declared always-on services.

## Why Process Launcher Exists (macOS TCC)

Process Launcher started as a solution to a macOS-specific problem. Apple's TCC (Transparency Consent and Control) framework restricts access to Microphone, Camera, Screen Recording, Accessibility, Full Disk Access, and protected folders. The system checks the responsible process and its GUI application ancestry. A plain background process spawned by cron, launchd, or PM2 has no GUI ancestor and therefore inherits no TCC permissions.

Process Launcher acts as a bridge. Start it from an interactive terminal (Terminal.app, iTerm2), and every job it launches inherits that terminal's TCC grants. All TCC-sensitive work goes through this one process, so you do not need each automation script to reconnect to a GUI session.

**This only works if the launcher itself runs inside an interactive GUI terminal session.** Background supervisors break the chain. See the Start section below for details.

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

Put real paths, service labels, `.env` references, and local SQLite paths only in the ignored local config or in a private overlay. Always-on services are declarative-only; do not create them through HTTP. Inspect them through the regular process and log endpoints. Use `POST /declared-services/{label}/restart` only for a service already present in YAML.

## Start The Launcher

**Important:** Start the launcher from an interactive terminal session. Terminal.app or iTerm2 works. tmux and zellij work only if the multiplexer server was itself started from one of those GUI terminals. If the server was started by launchd, SSH, or an automated script, TCC inheritance is broken.

```bash
# From an interactive terminal
process-launcher start --config config/launcher.yaml
```

The module entrypoint is equivalent:

```bash
python -m process_launcher start --config config/launcher.yaml
```

**Do not** start the launcher from cron, launchd, PM2, or any supervisor that lacks a GUI terminal ancestor. Jobs launched through those parents will not have TCC access, which is the main reason to use this tool on macOS.

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

Schedule and cancel a durable delayed launch:

```bash
curl -sf -X POST http://127.0.0.1:7997/run \
  -H 'Content-Type: application/json' \
  -d '{"command": ["python", "-c", "print(\"later\")"], "label": "later", "delay_seconds": 300}'

curl -sf -X POST http://127.0.0.1:7997/run \
  -H 'Content-Type: application/json' \
  -d '{"command": ["python", "daily.py"], "label": "absolute_time", "run_at": "2026-06-10T09:00:00-07:00", "misfire_policy": "run_immediately"}'

curl -sf http://127.0.0.1:7997/scheduled
curl -sf -X POST http://127.0.0.1:7997/scheduled/{job_id}/cancel
```

Scheduled jobs are persisted in SQLite and recovered on launcher restart. If `run_at` passed while the launcher was down, `misfire_policy` controls recovery: `run_immediately`, `skip`, or `fail`.

Inspect logs:

```bash
curl -sf http://127.0.0.1:7997/logs/heartbeat
curl -sf http://127.0.0.1:7997/logs/output
```

Restart a YAML-declared service:

```bash
curl -sf -X POST http://127.0.0.1:7997/declared-services/{label}/restart
```

There is no service list/create/reset API. Use `/processes` and logs to inspect declared services after startup or restart.

## Private Overlay Pattern

This skill is intentionally generic. Keep user-specific aliases, real job recipes, private domains, personal paths, and notification policies in a separate private skill or workspace file. The private overlay can call the public API examples here without modifying this repository.

## Safety Notes

`POST /run` executes commands as the launcher user. Use this service only on trusted localhost interfaces unless you add an authentication and authorization layer.
