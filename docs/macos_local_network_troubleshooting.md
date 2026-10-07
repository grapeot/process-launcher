# Troubleshooting macOS Local Network Privacy in Process Launcher Environments

## Process Liveness vs. Authorization Lifecycles

An active entry in a process supervisor table is not proof of useful work. Execution and authorization lifecycles can differ: after a parent interactive context terminates, surviving processes can continue running while losing effective access to local network devices.

Process Launcher starts child processes from an interactive terminal session to preserve a useful macOS privacy context. However, this is not a guarantee that authorization survives the hosting terminal's exit. Operators should check application output as well as process status when investigating stalled services.

## Observed Failure Pattern

In one observed environment, the failure presented through this sequence:

1. **Terminal Termination:** The terminal app received `SIGTERM` from an external `pkill` process and exited. The persistent multiplexer server and launcher survived, with the multiplexer reparented to `PPID 1` (launchd).
2. **Worker Degradation:** The application supervisor continued running, but capture workers repeatedly failed to connect and retried, producing no new output.
3. **Socket Denial:** Python TCP connections immediately returned `OSError: [Errno 65] No route to host`. Loopback communication remained functional. This narrowed the problem to LAN access, without establishing whether routing or privacy policy was responsible.
4. **System Tool Contrast:** `/usr/bin/curl` and `/usr/bin/nc` reached the same LAN device address and port. Running curl with `-q --noproxy '*'`, and checking local source and remote destination addresses, excluded an HTTP proxy explanation for the contrast.
5. **Persistence Across Reattachment:** A read-only probe submitted through `POST /run` also failed. Reattaching a new terminal client to the surviving multiplexer, or restarting a child service under the existing launcher, did not restore access. A new client connection does not recreate the existing multiplexer or launcher's execution context.
6. **SSH Comparison:** The same Python interpreter and target worked through a fresh SSH session to the same host. This was a Local Network diagnostic comparison, not evidence that SSH grants Microphone, Camera, Screen Recording, Accessibility, or Full Disk Access. Keychain authorization is a separate concern.

## Distinguishing Diagnostic Layers

These results distinguish process identity, launch context, and networking API choice:

| Test context | Mechanism | Observed result |
| --- | --- | --- |
| Surviving shell | Python BSD socket | `Errno 65 (EHOSTUNREACH)` |
| Same Python process | In-process system libcurl via `ctypes` | `CURLE_COULDNT_CONNECT (7)` |
| Same Python process | Spawned `/usr/bin/curl` | HTTP success |
| Diagnostic probe | Swift `NWConnection` | Unsatisfied path with `localNetworkDenied` |
| Fresh same-host SSH session | Same Python interpreter | TCP connection established |

### Generic Socket Error vs. Explicit Policy Denial

`EHOSTUNREACH` can reflect routing or neighbor-resolution failures as well as a policy denial. Curl success is a useful comparative clue, not standalone proof of a privacy problem. A probe using `Network.framework` provides explicit policy evidence when `NWConnection.currentPath?.unsatisfiedReason` is `localNetworkDenied`.

### In-Process Libraries vs. External Subprocesses

Loading system libcurl into Python did not make the connection work. Its error buffer reported a connection failure, without exposing an underlying errno. Spawning system curl from that same Python process did work.

Read-only process flag checks using `csops(CS_OPS_STATUS)` showed:

- Python: `CS_PLATFORM_BINARY = false`, `CS_VALID = true`.
- System curl: `CS_PLATFORM_BINARY = true`, `CS_VALID = true`.

[Apple TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy) explains that Local Network checks apply across BSD sockets, Network.framework, and URLSession, and that macOS tracks responsible code. In a public XNU snapshot, [necp_is_platform_binary](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/net/necp.c#L998-L1002) checks `csproc_get_platform_binary(proc) && cs_valid(proc)`. When platform-binary conditions apply, [socket policy information](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/net/necp.c#L9894-L9907) checks the owning process first and can then check its responsible process. It does not inspect which shared library called connect.

This supports a process-identity explanation for the library/executable difference. It does not expose the affected system's active policy rule: the public source snapshot predates that system, and runtime policy payloads were not inspected. A Developer ID or ad-hoc signature does not itself confer platform identity. Do not assume another curl build, a copied executable, or a re-signed executable behaves identically.

### Stored Preferences vs. Effective Access

System logs attributed blocked notifications to the former terminal process identity even after it exited, while `nehelper` reported that the terminal's stored preference allowed access. Thus, a preference showing Allowed was insufficient to establish effective access. The precise internal failure after terminal exit remains unverified.

TN3179 documents Local Network exceptions for launchd daemons, root programs, and command-line tools run from Terminal or SSH, including their children. These exceptions are narrower than the broader privacy privileges that Process Launcher's interactive-terminal deployment supports. They do not make SSH or root a universal replacement for that deployment model.

## Diagnostic Sequence and Verification Probe

Choose an authorized, read-only HTTP endpoint. Set `DEVICE_HOST`, optional `DEVICE_PORT` (default `80`), and optional `DEVICE_PATH` (default `/`). A non-HTTP service can accept TCP while failing this HTTP comparison; interpret the two results separately.

The probe uses only Python's standard library, discards response bodies, suppresses curl error text, and bounds both connection and subprocess duration:

```python
import os
import socket
import subprocess

host = os.environ["DEVICE_HOST"]
port = int(os.environ.get("DEVICE_PORT", "80"))
path = os.environ.get("DEVICE_PATH", "/")
if not path.startswith("/"):
    path = "/" + path
url_host = f"[{host}]" if ":" in host else host

try:
    with socket.create_connection((host, port), timeout=3):
        socket_status = "OK"
except OSError:
    socket_status = "FAILED"

command = [
    "/usr/bin/curl", "-q", "--noproxy", "*",
    "--connect-timeout", "3", "--max-time", "5",
    "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
    f"http://{url_host}:{port}{path}",
]
try:
    result = subprocess.run(command, capture_output=True, text=True, timeout=6)
    curl_status = f"HTTP {result.stdout.strip()} (exit {result.returncode})"
except subprocess.TimeoutExpired:
    curl_status = "TIMEOUT"
except OSError:
    curl_status = "EXEC_FAILED"

print(f"Socket: {socket_status} | curl: {curl_status}")
```

Compare the same target and interpreter in the existing shell, through the launcher's `POST /run`, and in a freshly opened GUI terminal outside the old multiplexer. If same-host SSH is already configured, it provides another launch-context comparison. A new GUI-terminal result is a test to perform, not a guaranteed recovery outcome.

Review recent policy messages:

```bash
/usr/bin/log show --last 5m --info --debug --style compact \
  --predicate 'subsystem == "com.apple.networkextension" AND (eventMessage CONTAINS[c] "local network" OR eventMessage CONTAINS[c] "LocalNetwork")'
```

Look for blocked notifications, responsible identities, and preference decisions near the probe time. Retained logs may not contain every event. Keep raw logs local; they can contain private application and device details.

For terminal termination, launchd records can distinguish a direct sender from secondary cleanup:

- `exited due to SIGTERM | sent by pkill[<pid>]` identifies the signal sender, not who invoked it or why.
- `during teardown of process-scoped services after host exited` identifies helper cleanup after the host exited.
- A `JetsamEvent` can involve system memory pressure or an individual process limit. Inspect the reason; `largestProcess` alone does not identify the process that was killed.

Arguments, parent-process records, or audit evidence are needed to attribute a batch termination command beyond its immediate sender.

## Recovery Guidance and Operational Boundaries

A human operator should first verify direct connectivity from a fresh GUI terminal outside the old multiplexer, then review active jobs before deliberately stopping and relaunching the launcher and affected workers in that verified context. A genuinely new multiplexer server is another option. The old server may host unrelated work and need not be terminated merely to create a new launcher context.

Agents must not restart the launcher from their own headless shell. This can interrupt workloads and cut off the control channel while recreating the same unsuitable launch context. Declared child-service restarts remain a separate operation, but did not repair this observed parent-context failure.

After recovery, verify output progress: new complete readable capture files, advancing worker counters, or fresh successful device reads. A healthy localhost API and running supervisor alone do not prove that LAN-dependent work resumed.

Spawning system curl was a useful temporary delegation mechanism for bounded HTTP requests in this environment. It did not grant Python broader permissions. HTTP delegation is not a drop-in replacement for persistent RTSP/RTP capture, session management, or media muxing; those require a separate transport design and validation. Local Network here means the LAN, not loopback localhost.
