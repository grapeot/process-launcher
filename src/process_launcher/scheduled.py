from __future__ import annotations

import asyncio
from datetime import datetime

from .models import MisfirePolicy, ProcessInfo, RunRequest, ScheduledJob, ScheduledStatus
from .process import ProcessManager
from .storage import SQLiteStore


class ScheduledManager:
    """Tracker and recovery scheduler for durable one-shot scheduled jobs."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self._jobs: dict[str, ScheduledJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        for job in self.store.list_scheduled_jobs():
            self._jobs[job.id] = job

    def add(self, job: ScheduledJob) -> None:
        self._jobs[job.id] = job
        self.store.upsert_scheduled_job(job)

    def set_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[job_id] = task

    def schedule(self, process_manager: ProcessManager, job: ScheduledJob, request: RunRequest, *, delay: float | None = None) -> None:
        actual_delay = delay if delay is not None else max((job.run_at - datetime.now()).total_seconds(), 0.0)
        task = asyncio.create_task(_delayed_run(process_manager, self, job, request, actual_delay))
        self.set_task(job.id, task)

    def mark_running(self, job_id: str, pid: int) -> None:
        job = self._jobs.get(job_id)
        if job:
            self._update_job(job.model_copy(update={"status": ScheduledStatus.RUNNING, "result_pid": pid, "started_at": datetime.now()}))

    def mark_completed(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            self._update_job(job.model_copy(update={"status": ScheduledStatus.COMPLETED, "completed_at": datetime.now()}))
        self._tasks.pop(job_id, None)

    def mark_failed(self, job_id: str, error: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            self._update_job(job.model_copy(update={"status": ScheduledStatus.FAILED, "last_error": error, "completed_at": datetime.now()}))
        self._tasks.pop(job_id, None)

    def mark_missed(self, job_id: str, error: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            self._update_job(job.model_copy(update={"status": ScheduledStatus.MISSED, "last_error": error, "completed_at": datetime.now()}))
        self._tasks.pop(job_id, None)

    def cancel(self, job_id: str) -> ScheduledJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status != ScheduledStatus.PENDING:
            raise ValueError(f"Cannot cancel job in status {job.status.value}")
        task = self._tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        self._update_job(job.model_copy(update={"status": ScheduledStatus.CANCELLED, "cancelled_at": datetime.now()}))
        return self._jobs[job_id]

    def list_jobs(self) -> list[ScheduledJob]:
        return sorted(self._jobs.values(), key=lambda j: j.scheduled_at)

    def recover_pending(self, process_manager: ProcessManager) -> None:
        self.store.mark_stale_running_jobs_failed()
        for job in self.store.list_scheduled_jobs():
            self._jobs[job.id] = job
        for job in self.store.list_pending_scheduled_jobs():
            if job.run_at > datetime.now():
                self.schedule(process_manager, job, self._request_from_job(job))
            elif job.misfire_policy == MisfirePolicy.RUN_IMMEDIATELY:
                self.schedule(process_manager, job, self._request_from_job(job), delay=0.0)
            elif job.misfire_policy == MisfirePolicy.SKIP:
                self.mark_missed(job.id, "scheduled run_at passed while launcher was down")
            elif job.misfire_policy == MisfirePolicy.FAIL:
                self.mark_failed(job.id, "scheduled run_at passed while launcher was down")

    def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()

    def close(self) -> None:
        self.store.close()

    def _update_job(self, job: ScheduledJob) -> None:
        self._jobs[job.id] = job
        self.store.update_scheduled_job(job)

    def _request_from_job(self, job: ScheduledJob) -> RunRequest:
        return RunRequest(
            command=job.command,
            cwd=job.cwd,
            env=job.env,
            label=job.label,
            timeout=job.timeout,
        )


async def _delayed_run(
    process_manager: ProcessManager,
    scheduled_manager: ScheduledManager,
    job: ScheduledJob,
    request: RunRequest,
    delay: float,
) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    scheduled_manager.mark_running(job.id, pid=0)
    try:
        response = await process_manager.start_process(
            request,
            on_exit=lambda info: _mark_scheduled_exit(scheduled_manager, job.id, info),
        )
        scheduled_manager.mark_running(job.id, pid=response.pid)
    except Exception as exc:
        scheduled_manager.mark_failed(job.id, str(exc))


def _mark_scheduled_exit(scheduled_manager: ScheduledManager, job_id: str, info: ProcessInfo) -> None:
    if info.exit_code == 0 and info.status.value == "exited":
        scheduled_manager.mark_completed(job_id)
    else:
        scheduled_manager.mark_failed(job_id, f"process exited with status={info.status.value} exit_code={info.exit_code}")
