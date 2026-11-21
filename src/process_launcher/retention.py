from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event, Thread

from .log import HeartbeatLogger, OutputLogger, to_iso8601, utc_now

logger = logging.getLogger(__name__)


class LogRetention:
    """Single worker per launcher; retention never runs on the API event loop."""

    def __init__(self, heartbeat: HeartbeatLogger, output: OutputLogger,
                 protected_output: Callable[[], set[Path]], interval: float) -> None:
        self._heartbeat = heartbeat
        self._output = output
        self._protected_output = protected_output
        self._interval = interval
        self._stop = Event()
        self._thread = Thread(target=self._run, name="log-retention", daemon=True)
        self._status: dict[str, object] = {"running": False, "last_started_at": None,
                                          "last_success_at": None, "last_error": None}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                logger.warning("Log retention worker is still stopping; a filesystem operation may be pending")

    def status(self) -> Mapping[str, object]:
        return {**self._status, "interval_seconds": self._interval}

    def _run(self) -> None:
        while not self._stop.is_set():
            self._status = {**self._status, "running": True, "last_started_at": to_iso8601(utc_now())}
            errors = []
            for name, sweep in (("heartbeat", self._clean_heartbeat), ("output", self._clean_output)):
                if self._stop.is_set():
                    break
                try:
                    sweep()
                except Exception as error:
                    logger.exception("%s log retention sweep failed", name)
                    errors.append(f"{name}: {error}")
            status = {**self._status, "running": False}
            if errors:
                status["last_error"] = "; ".join(errors)
            elif not self._stop.is_set():
                status.update(last_success_at=to_iso8601(utc_now()), last_error=None)
            self._status = status
            self._stop.wait(self._interval)

    def _clean_heartbeat(self) -> None:
        self._heartbeat.cleanup(stop=self._stop.is_set)

    def _clean_output(self) -> None:
        self._output.cleanup(protected=self._protected_output, stop=self._stop.is_set)
