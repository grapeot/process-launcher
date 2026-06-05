from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProcessStatus(str, Enum):
    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"
    CIRCUIT_BREAKER = "circuit_breaker"


class ProcessInfo(BaseModel):
    pid: int
    label: str | None = None
    command: str
    cwd: str | None = None
    status: ProcessStatus
    exit_code: int | None = None
    started_at: datetime
    exited_at: datetime | None = None
    output_file: str | None = None
    restart_count: int = 0


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: list[str] | str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    label: str | None = None
    timeout: float | None = Field(default=None, gt=0)
    delay_seconds: float | None = Field(default=None, ge=0, description="Delay execution by N seconds. Persisted across restarts.")
    run_at: datetime | None = Field(default=None, description="Absolute time to run the command.")
    misfire_policy: "MisfirePolicy" = Field(default="run_immediately")

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        if self.delay_seconds is not None and self.delay_seconds > 0 and self.run_at is not None:
            raise ValueError("delay_seconds and run_at cannot both be set")
        return self


class RunResponse(BaseModel):
    pid: int
    label: str | None = None
    started_at: datetime
    output_file: str | None = None


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7997


class LoggingConfig(BaseModel):
    dir: str = "logs"
    heartbeat_retention_days: int = 30
    output_retention_days: int = 30


class StorageConfig(BaseModel):
    sqlite_path: str = "state/launcher.db"


class ServiceConfig(BaseModel):
    label: str
    command: list[str] | str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    env_file: str | None = None
    restart_delay: float = 10.0
    max_restarts: int = 3
    restart_window: float = 60.0

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: list[str] | str) -> list[str] | str:
        if isinstance(value, list) and not value:
            raise ValueError("command must not be empty")
        if isinstance(value, str) and not value.strip():
            raise ValueError("command must not be empty")
        return value

    def resolved_env_file(self) -> Path | None:
        if not self.env_file:
            return None
        env_path = Path(self.env_file)
        if env_path.is_absolute() or not self.cwd:
            return env_path
        return Path(self.cwd) / env_path


class LauncherConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    services: dict[str, ServiceConfig] = Field(default_factory=dict)


class ScheduledStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MISSED = "missed"


class MisfirePolicy(str, Enum):
    RUN_IMMEDIATELY = "run_immediately"
    SKIP = "skip"
    FAIL = "fail"


class ScheduledJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    label: str | None = None
    command: list[str] | str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout: float | None = None
    scheduled_at: datetime
    run_at: datetime
    status: ScheduledStatus = ScheduledStatus.PENDING
    misfire_policy: MisfirePolicy = MisfirePolicy.RUN_IMMEDIATELY
    result_pid: int | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
