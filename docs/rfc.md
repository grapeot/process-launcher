# Process Launcher RFC

## Status

Draft for the public package scaffold.

## Design Summary

Process Launcher is a localhost FastAPI server around `subprocess.Popen`. It starts child processes, captures combined stdout and stderr, stores current process handles in memory, persists scheduled jobs in SQLite, and writes heartbeat events to JSONL files. It favors a small implementation over a broad job-management framework.

## Decisions

### D1: Localhost API

The service binds to `127.0.0.1` by default. `POST /run` accepts arbitrary commands, so network exposure would require authentication, authorization, and command policy work that is outside the current scope.

### D2: Process Handles Stay In Memory

Tracked process handles stay in memory. Restarting the launcher starts with an empty `/processes` table because a new process cannot safely inherit old `subprocess.Popen` handles. Durable run history can be added separately, but process control APIs only operate on children owned by the current launcher process.

### D3: SQLite-Backed Scheduled Jobs

Delayed and absolute-time scheduled jobs are stored in SQLite. YAML remains the bootstrap config for server, logging, storage, and declared always-on services. SQLite owns runtime lifecycle records for scheduled work: `pending`, `running`, `completed`, `failed`, `cancelled`, and `missed`.

The database includes a `schema_migrations` table. Each migration runs once and leaves a permanent version record so future launcher versions can upgrade older database files predictably.

### D4: External Recurrence

Recurring schedules stay outside the launcher. Cron, systemd timers, launchd, or any other scheduler can call `POST /run`. The launcher only handles immediate execution and one-shot durable delays.

### D5: Always-On Service Recovery

Always-on services use minimal restart logic because the launcher already owns the child process. Each service can set `restart_delay`, `max_restarts`, and `restart_window`. After repeated failures, the service enters `circuit_breaker` until a caller resets it.

Declared services come from YAML. API-created always-on services are intentionally unsupported. This avoids hidden long-lived daemons created through `/run` and keeps the launcher split clear: YAML owns service desired state; SQLite owns scheduled job lifecycle state.

### D6: Logs Are Local Files

Heartbeat events are JSONL files named by date. Output logs are one file per process start. Retention is controlled by simple day-count settings. The launcher does not upload, archive, or analyze logs.

### D7: OpenAPI Is the Integration Contract

FastAPI generates the OpenAPI schema. Agents and scripts can inspect `/openapi.json` instead of relying on a separate hand-maintained API schema.

### D8: TCC Foreground Process Constraint

The launcher must run inside an interactive terminal session to inherit macOS TCC permissions. This is not a software limitation. It is a consequence of how macOS privacy controls audit the responsible process. The launcher is designed as a TCC bridge: start it once from a GUI terminal, then route all TCC-sensitive jobs through its API.

Background supervisors (cron, launchd, PM2) cannot provide the required GUI ancestry. Multiplexers (tmux, zellij) work only if their server was started from an interactive GUI terminal. This constraint is a deployment requirement, not a feature gap.

## Data Flow

```text
caller -> POST /run -> ProcessManager -> subprocess.Popen
                                      -> output log file
                                      -> heartbeat JSONL

caller -> POST /run with delay_seconds/run_at -> SQLite scheduled_jobs
                                             -> recovery scheduler
                                             -> ProcessManager at run_at

caller -> GET /processes/{pid} -> in-memory process table
caller -> GET /scheduled -> SQLite scheduled_jobs
caller -> GET /logs/output/{file} -> local output log
```

## Startup Recovery

On startup, the launcher opens SQLite, applies pending migrations, marks stale `running` scheduled jobs as `failed`, then reloads `pending` scheduled jobs.

For each pending job:

- `run_at > now`: register an asyncio task for the remaining delay.
- `run_at <= now` and `misfire_policy = run_immediately`: run as soon as recovery completes.
- `run_at <= now` and `misfire_policy = skip`: mark `missed`.
- `run_at <= now` and `misfire_policy = fail`: mark `failed`.

Completed, failed, cancelled, and missed jobs stay in SQLite for inspection and are not rescheduled.

## Security Model

The service is a trusted-local tool. A caller that can reach the API can run commands as the launcher user. Public deployments or shared machines need an additional security layer before exposing the API beyond localhost.

macOS TCC adds a deployment-level security constraint. The launcher must be started from an interactive GUI terminal to grant its children access to TCC-protected capabilities. Background startup methods (cron, launchd, PM2) bypass the GUI ancestry chain and will cause TCC-sensitive jobs to fail silently or with denied-permission errors.

## Public And Private Layers

The public package owns generic code and docs. Private overlays own real job recipes, local service aliases, personal paths, and secrets. This separation is part of the project design rather than an afterthought.
