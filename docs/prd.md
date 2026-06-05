# Process Launcher PRD

## Problem

Local automation often needs a small trusted process to start commands, track their lifecycle, and expose status to scripts or AI agents. Shell scripts can start commands, but they do not provide a reusable API for process status, output lookup, delayed launch cancellation, or simple always-on service recovery.

On macOS, an additional constraint comes from TCC (Transparency Consent and Control) privacy controls. The system restricts access to Microphone, Camera, Screen Recording, Accessibility, Full Disk Access, and protected folders. These checks look at the responsible process and its GUI application ancestry. A child process only inherits TCC permissions if its parent chain leads back to a trusted GUI application that the user has granted permission to.

Process Launcher provides both the control surface and a TCC permission bridge. It is intentionally narrow: receive a command, start a child process, capture output, record lifecycle events, expose current process state, and persist delayed jobs that must survive launcher restarts. Started from an interactive terminal, every child it launches inherits that terminal's TCC grants.

## Goals

- Start one-off commands through `POST /run`.
- Track process status, exit code, timestamps, labels, and output file paths.
- Capture stdout and stderr into local log files.
- Record heartbeat events as JSONL for audit and troubleshooting.
- Support durable delayed launches that can be listed, cancelled while pending, and recovered after launcher or machine restarts.
- Support absolute `run_at` scheduling for cross-day jobs.
- Support a small set of configured always-on services with restart and circuit-breaker behavior.
- Provide an OpenAPI schema for human scripts and AI agents.

## Non-Goals

- Process Launcher is not a distributed job queue.
- It does not recover arbitrary child process handles across restarts.
- It does not implement recurring schedules; use cron, systemd timers, or another scheduler to call the API.
- It does not replace YAML configuration with SQLite. YAML remains the bootstrap and declared-service source.
- It does not persist API-created always-on services in this version.
- It does not send notifications.
- It does not sandbox commands.
- It is not designed to be exposed to untrusted networks.

## Users

The primary users are local developers, automation scripts, and AI agents that need a predictable API for running local commands. The service assumes that callers on localhost are trusted.

## Configuration

Runtime configuration lives in `config/launcher.yaml`, which is ignored by git. The repository ships `config/launcher.example.yaml` with fake placeholders.

```yaml
server:
  host: 127.0.0.1
  port: 7997

logging:
  dir: logs
  heartbeat_retention_days: 30
  output_retention_days: 30

storage:
  sqlite_path: state/launcher.db

services:
  demo_service:
    label: demo_service
    command: ["python", "-m", "http.server", "8080"]
    cwd: /path/to/demo/project
    env:
      DEMO_MODE: development
    restart_delay: 10
    max_restarts: 3
    restart_window: 60
```

Private paths, real service names, secrets, and local job recipes belong in the ignored local config or in a separate private overlay.

## API Surface

- `GET /health` reports server liveness.
- `POST /run` starts a command immediately or creates a durable scheduled launch with `delay_seconds` or `run_at`.
- `GET /scheduled` lists delayed jobs.
- `POST /scheduled/{job_id}/cancel` cancels a pending delayed job.
- `GET /processes` lists tracked processes.
- `GET /processes/{pid}` returns one tracked process.
- `POST /processes/{pid}/stop` terminates a tracked process.
- `GET /processes/{pid}/output` reads captured output.
- `GET /services` lists configured or ad hoc always-on services.
- `POST /services/{label}/restart` restarts a service.
- `POST /services/{label}/reset` resets a service circuit breaker.
- `GET /logs/heartbeat` reads heartbeat events.
- `GET /logs/output` lists output log files.
- `GET /logs/output/{filename}` reads one output log file.
- `GET /openapi.json` exposes the generated OpenAPI schema.

## Private Overlay Pattern

The public repository contains the generic tool, tests, docs, and skill. A private workspace can maintain overlays such as job aliases, real service recipes, local paths, private domains, notification policies, and operational runbooks. This keeps the package reusable while allowing a user-specific workflow to stay private.

## Durable Scheduling

Delayed jobs are stored in SQLite under `storage.sqlite_path`, relative to the config base directory when a relative path is used. On startup, Process Launcher reloads `pending` jobs and schedules them again. `completed`, `failed`, `cancelled`, and `missed` jobs remain queryable but are not reloaded.

If a pending job's `run_at` time has already passed while the launcher was down, `misfire_policy` decides what happens:

- `run_immediately` starts the job during recovery.
- `skip` marks the job as `missed`.
- `fail` marks the job as `failed` with an explanatory error.

If the launcher restarts while a scheduled job is `running`, the next startup marks that job `failed` because the new launcher process no longer owns a reliable process handle.
