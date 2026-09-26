# Agentic Patrol Skill

## Purpose

Use this skill when an AI agent needs to monitor a long-running, asynchronous task by performing periodic inspections, evaluating live task state, and deciding dynamically whether to stop or schedule **one** future inspection.

In this pattern, the agent drives its own check-in cadence using Process Launcher's one-shot delayed execution. Instead of relying on a rigid, recurring cron loop or an external supervisor daemon, the agent:
1. Inspects the current state of the running task.
2. Decides whether the task has concluded or requires intervention.
3. If still active, schedules one delayed inspection and passes a stable task-specific prompt back to itself.

A one-shot delayed schedule can be preferable to a fixed cron loop:
- **Dynamic Cadence**: The agent adapts inspection intervals based on observed progress.
- **Inspect-Then-Decide**: Every inspection explicitly assesses whether further monitoring is justified.
- **Natural Termination**: Once the task reaches its defined stopping condition, the agent refrains from scheduling another follow-up.

Process Launcher provides durable time-delayed command execution and SQLite persistence across restarts, but it maintains no agentic state machine, supervisor, or reconciler. This is an intentional best-effort, prompt-driven self-scheduling recipe.

## When to Use

- **Long-Running Async Workflows**: Multi-stage software builds, dataset downloads, video encodings, model training jobs, database migrations, or remote benchmark runs that take minutes to hours.
- **Variable or Unpredictable Durations**: Tasks where completion times cannot be forecasted reliably, making static sleep timers inappropriate.
- **Evaluation-Driven Decisions**: Workflows where interim artifacts, partial logs, or health metrics must be evaluated to detect stalls, degradation, or errors early.

Do **not** use this pattern for:
- Static, single-alert reminders with no inspection logic (use standard same-session reminders).
- High-frequency, sub-minute telemetry polling (use dedicated metrics tooling).
- Mission-critical workflows requiring guaranteed watchdog SLAs (a Process Launcher child exit code of 0 does not certify that an LLM successfully executed the turn).
- Always-on background daemons (use Process Launcher YAML-declared `services`).

## Contract

Each inspection uses a stable, task-specific prompt containing its evidence sources, stopping conditions, bounded exceptions, allowed actions, and next-inspection policy. The prompt is the task contract; the scheduled command is only its wake-up transport.

1. **Inspect Real Task State**
   - Check concrete, authoritative task indicators: process exit statuses, log file lines, database rows, generated output files, or API response bodies.
   - Never assume elapsed wall-clock time corresponds to real progress.

2. **Stop if Complete or Terminal**
   - **Success**: Verify the task-specific success criteria; a process exiting cleanly may be insufficient.
   - **Explicit Bounded Terminal Exception**: Stop or ask for human intervention when a defined, unrecoverable failure or bounded stall condition occurs. A retryable failure need not end the patrol.
   - **Human Stop**: The user issued an explicit cancellation or paused the workflow.
   - **Deadline Exceeded**: The task exceeded an agreed maximum runtime or inspection count.
   - **Action**: On a terminal condition, report the result to the agent session and **do not schedule another job**.

3. **Schedule One Follow-Up**
   - If the task still requires inspection, determine the appropriate delay (`delay_seconds` or ISO `run_at`).
   - Query `GET /scheduled?status=pending&label=<label>` first to avoid a duplicate pending patrol job. The `label` is a substring filter, not a uniqueness constraint; verify the command and target time before assuming a match.
   - Dispatch `POST /run` with a one-shot delay to invoke the harness CLI with the stable prompt file.
   - For asynchronous handoff CLIs such as the OpenCode example below, do not pass blocking flags such as `--wait`. Other harnesses have their own execution contract.

### Process Launcher Boundary

- **`POST /run`** accepts a `command` (argv array preferred), `cwd`, `label`, `timeout`, and either `delay_seconds` or ISO `run_at`. Optional `misfire_policy` values are `run_immediately`, `skip`, and `fail`. Delayed runs initially return an immediate response with `pid: 0` and `output_file: null`.
- **Persistence**: Scheduled jobs are persisted in SQLite and recover after a launcher restart subject to the misfire policy. Process Launcher does **not** enforce a single pending patrol job or provide an exactly-once guarantee.
- **Handoff status**: A scheduled job marked `completed` means its launched child process exited with code 0. It does **not** certify that the downstream agent executed the inspection or scheduled the next step.
- **Diagnostics**: Check `GET /scheduled/{id}` or `GET /processes/{pid}/output` for dispatch errors. If a handoff times out or reports an ambiguous status, inspect the target harness session timeline before rescheduling to avoid duplicate turns.
- **Prompt storage**: Keep the task-specific patrol prompt at a stable, private path (for example, `/opt/example/agent-cli/prompts/patrol_task.md`), not in a temporary directory subject to cleanup.
- **Authorization**: This skill grants no standing authority to create schedules, send external alerts, or perform irreversible repairs. Follow the user's authorization for each action; do not embed secrets in scheduled commands or public examples.

## OpenCode Example

This is a reference implementation specific to OpenCode, not the universal Process Launcher contract. For OpenCode-specific session identification, submission routing, and error handling, consult the separate `skill_opencode_submission.md` skill.

### 1. Preflight Validation

Before scheduling, the agent runs a dry-run check to verify the target session ID and routing. The dry-run exercises an ephemeral OK-only check without appending content to the target session:

```bash
/opt/example/agent-cli/.venv/bin/python -m opencode_skill append \
  --session-id ses_example_12345 \
  --prompt-file /opt/example/agent-cli/prompts/patrol_worker.md \
  --dry-run
```

### 2. Check Pending Reservations

```bash
curl -sf 'http://127.0.0.1:7997/scheduled?status=pending&label=patrol_build_42'
```

Inspect the returned JSON array. Because `label` matches substrings, verify that any pending item has the matching session ID, command, and intended time before deciding a duplicate exists.

### 3. Schedule the Follow-Up

The agent calls `POST /run` on Process Launcher. The invocation omits `--wait`, so the scheduled command returns after the append handoff, not after the agent turn:

```bash
curl -sf -X POST http://127.0.0.1:7997/run \
  -H 'Content-Type: application/json' \
  -d '{
    "command": [
      "/opt/example/agent-cli/.venv/bin/python",
      "-m",
      "opencode_skill",
      "append",
      "--session-id",
      "ses_example_12345",
      "--prompt-file",
      "/opt/example/agent-cli/prompts/patrol_worker.md",
      "--send-timeout",
      "5",
      "--json"
    ],
    "cwd": "/opt/example/agent-cli",
    "label": "patrol_build_42",
    "delay_seconds": 600,
    "timeout": 60,
    "misfire_policy": "run_immediately"
  }'
```

When the timer fires, Process Launcher runs `opencode_skill append`; the resumed agent then follows its stable prompt: inspect, decide, and only if needed schedule the next one-shot job. Do not infer the inspection succeeded merely because the append process exited 0. An ambiguous handoff must be checked against the OpenCode session before scheduling again.

## Other Harnesses

Different agent harnesses may not have a native same-session append CLI. Consult the separate `ai-agent-cli` root skill (`skill_ai_agent_cli.md`) for the chosen harness's verified, file-based invocation; do not invent session-continuation flags. If the harness starts a new session for every inspection, give it a stable prompt and pointer to the real task state so it can recover context without prior chat history.

The Launcher role stays the same: execute a delayed command array at the designated time. The task prompt decides whether to inspect again.

## Failure Boundaries

- **Broken chain**: The agent may fail to answer or forget to schedule another check. This pattern accepts that risk; it is not a guaranteed watchdog. No recovery daemon or additional state store is implied.
- **Ambiguous dispatch**: The CLI might time out even though the target harness received the prompt. Inspect its session and command output before retrying to avoid duplicate turns.
- **Accidental overlap**: A one-shot schedule and a pre-scheduling check do not prevent concurrent agent turns or make label matching atomic. Check existing jobs and live task locks; do not assume exactly-once execution.
- **Runaway patrol**: Define a human stop path and bounded deadline in the task-specific prompt. Stop scheduling once the agreed goal, terminal exception, or deadline is reached.
