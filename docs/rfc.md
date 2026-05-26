# Process Launcher RFC

## Status

Draft for the public package scaffold.

## Design Summary

Process Launcher is a localhost FastAPI server around `subprocess.Popen`. It starts child processes, captures combined stdout and stderr, stores lifecycle state in memory, and writes heartbeat events to JSONL files. It favors a small implementation over a broad job-management framework.

## Decisions

### D1: Localhost API

The service binds to `127.0.0.1` by default. `POST /run` accepts arbitrary commands, so network exposure would require authentication, authorization, and command policy work that is outside the current scope.

### D2: In-Memory State

Tracked processes and delayed jobs are stored in memory. Restarting the launcher starts with an empty process table. This matches the tool's role as a local process launcher rather than a durable queue.

### D3: External Scheduling

Recurring schedules stay outside the launcher. Cron, systemd timers, launchd, or any other scheduler can call `POST /run`. The launcher only handles immediate execution and simple in-memory delays.

### D4: Always-On Service Recovery

Always-on services use minimal restart logic because the launcher already owns the child process. Each service can set `restart_delay`, `max_restarts`, and `restart_window`. After repeated failures, the service enters `circuit_breaker` until a caller resets it.

### D5: Logs Are Local Files

Heartbeat events are JSONL files named by date. Output logs are one file per process start. Retention is controlled by simple day-count settings. The launcher does not upload, archive, or analyze logs.

### D6: OpenAPI Is the Integration Contract

FastAPI generates the OpenAPI schema. Agents and scripts can inspect `/openapi.json` instead of relying on a separate hand-maintained API schema.

## Data Flow

```text
caller -> POST /run -> ProcessManager -> subprocess.Popen
                                      -> output log file
                                      -> heartbeat JSONL

caller -> GET /processes/{pid} -> in-memory process table
caller -> GET /logs/output/{file} -> local output log
```

## Security Model

The service is a trusted-local tool. A caller that can reach the API can run commands as the launcher user. Public deployments or shared machines need an additional security layer before exposing the API beyond localhost.

## Public And Private Layers

The public package owns generic code and docs. Private overlays own real job recipes, local service aliases, personal paths, and secrets. This separation is part of the project design rather than an afterthought.
