from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .log import HeartbeatLogger, OutputLogger, to_iso8601, utc_now
from .models import ProcessInfo, ProcessStatus, RunRequest, RunResponse

ExitCallback = Callable[[ProcessInfo], Awaitable[None] | None]


@dataclass
class TrackedProcess:
    popen: subprocess.Popen[str]
    info: ProcessInfo
    output_path: Path
    process_group_id: int | None = None
    stop_requested: bool = False
    timeout_task: asyncio.Task[None] | None = None
    output_thread: threading.Thread | None = None
    watcher_thread: threading.Thread | None = None


class ProcessManager:
    def __init__(self, heartbeat_logger: HeartbeatLogger, output_logger: OutputLogger) -> None:
        self.heartbeat_logger = heartbeat_logger
        self.output_logger = output_logger
        self.processes: dict[int, TrackedProcess] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start_process(
        self,
        request: RunRequest,
        *,
        restart_count: int = 0,
        on_exit: ExitCallback | None = None,
    ) -> RunResponse:
        self._loop = asyncio.get_running_loop()
        started_at = utc_now()
        output_path = self.output_logger.create_output_file(request.label, started_at)
        if isinstance(request.command, str):
            use_shell = True
            popen_args = request.command
            display_command = request.command
        else:
            use_shell = False
            popen_args = list(request.command)
            display_command = " ".join(popen_args)
        env = os.environ.copy()
        env.update(request.env)

        popen = subprocess.Popen(
            popen_args,
            cwd=request.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=use_shell,
            start_new_session=os.name == "posix",
        )
        pid = popen.pid
        info = ProcessInfo(
            pid=pid,
            label=request.label,
            command=display_command,
            cwd=request.cwd,
            status=ProcessStatus.RUNNING,
            started_at=started_at,
            output_file=str(output_path),
            restart_count=restart_count,
        )
        handle = TrackedProcess(
            popen=popen,
            info=info,
            output_path=output_path,
            process_group_id=pid if os.name == "posix" else None,
        )
        with self._lock:
            self.processes[pid] = handle

        self.heartbeat_logger.write_event(
            "PROCESS_STARTED",
            pid=pid,
            label=request.label,
            command=info.command,
            cwd=request.cwd,
            output_file=str(output_path),
            restart_count=restart_count,
        )

        output_thread = threading.Thread(target=self._stream_output, args=(handle,), daemon=True)
        handle.output_thread = output_thread
        output_thread.start()
        watcher = threading.Thread(target=self._wait_for_exit, args=(handle, on_exit), daemon=True)
        handle.watcher_thread = watcher
        watcher.start()

        if request.timeout:
            handle.timeout_task = asyncio.create_task(self._enforce_timeout(pid, request.timeout))

        return RunResponse(pid=pid, label=request.label, started_at=started_at, output_file=str(output_path))

    async def stop_process(self, pid: int, *, grace_period: float = 20.0) -> ProcessInfo:
        handle = self._get_handle(pid)
        if handle.info.status != ProcessStatus.RUNNING:
            return handle.info

        handle.stop_requested = True
        self._signal_process_tree(handle)
        if not await self._wait_for_process_tree(handle, grace_period):
            self._signal_process_tree(handle, force=True)
            await self._wait_for_process_tree(handle, 5.0)
        await asyncio.to_thread(handle.popen.wait)
        await self._join_handle_threads(handle)
        return handle.info

    async def stop_all(self, *, grace_period: float = 20.0) -> None:
        running = [pid for pid, handle in self.processes.items() if handle.info.status == ProcessStatus.RUNNING]
        for pid in running:
            await self.stop_process(pid, grace_period=grace_period)
        for handle in list(self.processes.values()):
            await self._join_handle_threads(handle)

    async def _join_handle_threads(self, handle: TrackedProcess, timeout: float = 5.0) -> None:
        for thread in (handle.output_thread, handle.watcher_thread):
            if thread is not None and thread.is_alive():
                await asyncio.to_thread(thread.join, timeout)

    def _signal_process_tree(self, handle: TrackedProcess, *, force: bool = False) -> None:
        try:
            if handle.process_group_id is not None:
                sig = signal.SIGKILL if force else signal.SIGTERM
                os.killpg(handle.process_group_id, sig)
            elif force:
                handle.popen.kill()
            else:
                handle.popen.terminate()
        except ProcessLookupError:
            return

    def _process_tree_exists(self, handle: TrackedProcess) -> bool:
        if handle.process_group_id is None:
            return handle.popen.poll() is None
        try:
            os.killpg(handle.process_group_id, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Darwin can return EPERM for a recently emptied group. Confirm
            # membership instead of treating EPERM itself as an exit signal.
            result = subprocess.run(
                ["ps", "-axo", "pgid="],
                check=True,
                capture_output=True,
                text=True,
            )
            return any(line.strip() == str(handle.process_group_id) for line in result.stdout.splitlines())

    async def _wait_for_process_tree(self, handle: TrackedProcess, timeout: float) -> bool:
        if handle.process_group_id is None:
            try:
                await asyncio.to_thread(handle.popen.wait, timeout)
            except subprocess.TimeoutExpired:
                return False
            return True

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not self._process_tree_exists(handle):
                return True
            await asyncio.sleep(0.05)
        return False

    def list_processes(self, *, running_only: bool = False) -> list[ProcessInfo]:
        items = [handle.info for handle in self.processes.values()]
        if running_only:
            items = [info for info in items if info.status == ProcessStatus.RUNNING]
        return sorted(items, key=lambda info: info.started_at)

    def get_process(self, pid: int) -> ProcessInfo | None:
        handle = self.processes.get(pid)
        return handle.info if handle else None

    def get_output(self, pid: int, tail: int | None = None) -> dict[str, object]:
        handle = self._get_handle(pid)
        output_path = Path(handle.info.output_file or handle.output_path)
        lines = output_path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[-tail:] if tail else lines
        return {
            "content": "\n".join(selected),
            "total_lines": len(lines),
            "file": str(output_path),
        }

    def _get_handle(self, pid: int) -> TrackedProcess:
        handle = self.processes.get(pid)
        if handle is None:
            raise KeyError(pid)
        return handle

    def _stream_output(self, handle: TrackedProcess) -> None:
        stdout = handle.popen.stdout
        if stdout is None:
            return
        with handle.output_path.open("a", encoding="utf-8") as output_file:
            for line in iter(stdout.readline, ""):
                output_file.write(line)
                output_file.flush()
        stdout.close()

    def _wait_for_exit(self, handle: TrackedProcess, on_exit: ExitCallback | None) -> None:
        exit_code = handle.popen.wait()
        while self._process_tree_exists(handle):
            time.sleep(0.05)
        if handle.output_thread is not None:
            handle.output_thread.join(timeout=5.0)
        exited_at = utc_now()
        duration = max((exited_at - handle.info.started_at).total_seconds(), 0.0)
        if handle.stop_requested:
            status = ProcessStatus.KILLED
        else:
            status = ProcessStatus.EXITED

        handle.info = handle.info.model_copy(
            update={
                "status": status,
                "exit_code": exit_code,
                "exited_at": exited_at,
            }
        )
        with self._lock:
            self.processes[handle.info.pid] = handle

        self.heartbeat_logger.write_event(
            "PROCESS_EXITED",
            pid=handle.info.pid,
            label=handle.info.label,
            exit_code=exit_code,
            duration_s=duration,
            output_file=handle.info.output_file,
            status=status.value,
            exited_at=to_iso8601(exited_at),
        )

        if handle.timeout_task is not None:
            handle.timeout_task.cancel()

        if on_exit and self._loop is not None and not self._loop.is_closed():
            result = on_exit(handle.info)
            if asyncio.iscoroutine(result):
                self._loop.call_soon_threadsafe(asyncio.create_task, result)

    async def _enforce_timeout(self, pid: int, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            handle = self.processes.get(pid)
            if handle and handle.info.status == ProcessStatus.RUNNING:
                await self.stop_process(pid, grace_period=1.0)
        except asyncio.CancelledError:
            return
