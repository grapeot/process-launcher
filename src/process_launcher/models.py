from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


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
    command: list[str] | str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    label: str | None = None
    always_on: bool = False
    timeout: float | None = Field(default=None, gt=0)
    delay_seconds: float | None = Field(default=None, ge=0, description="Delay execution by N seconds. Lost on restart.")


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
    services: dict[str, ServiceConfig] = Field(default_factory=dict)


class ScheduledStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ScheduledJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    label: str | None = None
    command: str
    cwd: str | None = None
    scheduled_at: datetime
    run_at: datetime
    status: ScheduledStatus = ScheduledStatus.PENDING
    result_pid: int | None = None
