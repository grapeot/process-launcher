# Working Notes

## Changelog

- 2026-10-29: Service diagnosis-policy propagation: `ServiceConfig.diagnose_on_failure` (default `true`) → `ServiceMonitor._start_service` → `RunRequest`. Coverage: YAML default/true/false, initial start, automatic retry, explicit restart, manual stop, process metadata, start/exit heartbeat policy, real exit code 7, output capture, circuit breaker and reset. Service `RESTART`, `CIRCUIT_BREAKER`, and `CIRCUIT_BREAKER_RESET` events preserve the policy.
  - Regression evidence before fix: 2 failed / 2 passed. Final offline verification: 127 passed / 9 live-integration tests deselected; module entrypoint help passed. Independent review and follow-up review: PASS, no blockers; targeted reviewer verification: 4 passed. New test waits for the matching exit heartbeat before asserting event history. Live shared launcher: unchanged; operator restart and post-restart acceptance pending. Merge gate: live acceptance, not offline tests alone.
- 2026-10-28: Documentation-only amendment requiring explicit diagnosis opt-outs for temporary dev/preview servers, local visual iteration, one-off QA, probes, and expected failures. Added live-schema preflight, a prohibition on dropping rejected policy fields, metadata readback, and safe fallback/blocker guidance. Read-only inspection found a legacy live API without the policy field or a running-process mutation endpoint; no runtime policy changed.
  - Validation: offline suite (`python -m pytest -q -m 'not live_integration'`) passed with 123 tests and 9 deselected; module entrypoint help passed. A fresh independent reviewer checked the diff against API, scheduling, periodic, and process lifecycle source and returned PASS with no blocking findings.
- 2026-10-24: Added `diagnose_on_failure` (boolean, default `true`) across immediate and delayed `POST /run`, pending `PATCH /scheduled/{id}` (including single-field patches), and YAML `periodic_jobs` declarations. The policy propagates to responses, process metadata, start/exit heartbeat events, and SQLite schema v3 scheduled jobs and periodic run snapshots. Historical rows default to `true`; recovery preserves opt-outs and reload preserves in-flight snapshots. `false` leaves failure status, logs, timeouts, callbacks, and scheduling intact while signaling external diagnostic consumers to skip automatic diagnosis (missing fields mean `true`).

## Repository Contract

Tracked files should be safe for public release. Runtime files are local-only:

- `config/launcher.yaml`
- `.env`
- `logs/`
- caches and build outputs

Use `config/launcher.example.yaml` and `.env.example` for fake examples. Keep private overlays outside the repository.

## Development Setup

```bash
uv venv
uv pip install -e '.[dev]'
```

Run the CLI with either entrypoint:

```bash
process-launcher start --config config/launcher.yaml
python -m process_launcher start --config config/launcher.yaml
```

## TCC-Aware Deployment

The launcher must be started from an interactive terminal session to act as a macOS TCC permission bridge. When deploying:

- Start from Terminal.app, iTerm2, or a tmux/zellij server that was itself started from one of those.
- Do not use cron, launchd, or PM2 as the launcher's parent for TCC-sensitive jobs.
- Verify TCC permissions manually after deployment (unit tests cannot simulate the GUI ancestry chain).
- If you need launchd scheduling with TCC access, wrap the job in a signed `.app` bundle with a stable bundle ID and the required entitlements, then have the bundle call the launcher API or launch a TCC-signed helper.

## Process Lifecycle

- Each launched command runs in its own process group on POSIX systems.
- Stop and restart operations signal the entire group, allow 20 seconds for graceful cleanup, and only then escalate to `SIGKILL`.
- A process remains tracked until its complete group exits, even if the original group leader exits first. Execution timeouts retain a one-second termination grace rather than the longer manual-stop grace, and bulk shutdown signals all groups concurrently.
- This prevents shell wrappers or supervisors from exiting while their ffmpeg or worker descendants continue as orphans.

## Release Checklist

- Search tracked public files for private paths, real emails, private domains, and secrets.
- Run the unit test suite.
- Run the package entrypoint smoke check.
- Confirm `config/launcher.yaml`, `.env`, and `logs/` are ignored.
- Confirm docs describe the private overlay pattern rather than embedding private recipes.

## Troubleshooting Notes

### Checkout Features May Not Be Live

A long-lived launcher can serve an older request schema even after the checkout gains a new field. Inspect live `GET /openapi.json` before relying on `diagnose_on_failure: false`; a missing or rejected field is a blocker, not permission to retry without it. Pending-job patches and periodic reloads cannot change the policy of an already running process. Record the unresolved opt-out and preserve a still-needed process until a safe alternative is available.

### macOS Local Network Privacy and Process Lifecycles

A surviving launcher or multiplexer can remain running while LAN-dependent workers stop making progress after the hosting terminal exits. Reattaching a new terminal client to an old multiplexer does not recreate its launch context.

- Compare the same interpreter and target in the existing shell, through `/run`, and in a fresh terminal. Curl success is a clue; `NWConnection` reporting `localNetworkDenied` supplies explicit policy evidence.
- System curl's process identity can differ from Python's. Loading libcurl in-process is not equivalent to spawning `/usr/bin/curl`, and other curl builds are not guaranteed to behave identically.
- Recovery belongs to a human operator in a freshly verified context. Review active jobs before restarting the launcher, and verify application output rather than process liveness alone.

See [macOS Local Network troubleshooting](macos_local_network_troubleshooting.md) for the generalized observations, public-source mechanism analysis, read-only probe, and recovery guidance.
