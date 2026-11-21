from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from threading import Lock
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso8601(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class HeartbeatLogger:
    def __init__(self, base_dir: str | Path, retention_days: int = 30) -> None:
        self.base_dir = Path(base_dir)
        self.retention_days = retention_days
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _file_for_date(self, ts: datetime) -> Path:
        return self.base_dir / f"heartbeat_{ts.astimezone(UTC).date().isoformat()}.jsonl"

    def write_event(self, event: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            now = utc_now()
            record = {"ts": to_iso8601(now), "event": event, **payload}
            with self._file_for_date(now).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def cleanup(self, *, stop: Callable[[], bool] = lambda: False) -> int:
        def remove(path: Path, cutoff: float) -> bool:
            if not _is_expired(path, cutoff):
                return False
            with self._lock:
                return path != self._file_for_date(utc_now()) and _remove_expired(path, cutoff, stop)

        cutoff = (utc_now() - timedelta(days=self.retention_days)).timestamp()
        return _sweep(self.base_dir, "heartbeat_*.jsonl", cutoff, remove, stop)

    def read_events(
        self,
        *,
        limit: int = 100,
        event: str | None = None,
        label: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in sorted(self.base_dir.glob("heartbeat_*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue  # Retention may remove it after the directory listing.
            for line in lines:
                if not line.strip():
                    continue
                record = json.loads(line)
                if event and record.get("event") != event:
                    continue
                if label and record.get("label") != label:
                    continue
                if since:
                    ts = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
                    if ts < since:
                        continue
                events.append(record)
        events.sort(key=lambda item: item["ts"])
        return events[-limit:]


class OutputLogger:
    def __init__(self, base_dir: str | Path, retention_days: int = 30) -> None:
        self.base_dir = Path(base_dir)
        self.retention_days = retention_days
        self.output_dir = self.base_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def cleanup(self, *, protected: Callable[[], Collection[Path]],
                stop: Callable[[], bool] = lambda: False) -> int:
        # Take the cutoff before the snapshot: files created after the snapshot are fresh.
        cutoff = (utc_now() - timedelta(days=self.retention_days)).timestamp()
        active_paths = protected()

        def remove(path: Path, cutoff: float) -> bool:
            if path in active_paths or not _is_expired(path, cutoff):
                return False
            with self._lock:
                return _remove_expired(path, cutoff, stop)

        return _sweep(self.output_dir, "*.log", cutoff, remove, stop)

    def create_output_file(self, label: str | None, started_at: datetime) -> Path:
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label or "process")
        stamp = started_at.astimezone(UTC).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"{safe_label}_{stamp}.log"
        with self._lock:
            path.touch()
        return path

    def list_output_logs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.output_dir.glob("*.log")):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            items.append(
                {
                    "filename": path.name,
                    "label": path.stem.rsplit("_", 2)[0],
                    "created_at": to_iso8601(datetime.fromtimestamp(stat.st_mtime, tz=UTC)),
                    "size_bytes": stat.st_size,
                }
            )
        return items

    def read_output_log(self, filename: str, tail: int | None = None) -> dict[str, Any]:
        path = self.output_dir / filename
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(filename)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[-tail:] if tail else lines
        return {
            "content": "\n".join(selected),
            "total_lines": len(lines),
            "file": str(path),
            "filename": filename,
        }


def _is_expired(path: Path, cutoff: float) -> bool:
    metadata = path.lstat()
    return stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < cutoff


def _remove_expired(path: Path, cutoff: float, stop: Callable[[], bool]) -> bool:
    if stop() or not _is_expired(path, cutoff) or stop():
        return False
    path.unlink()
    return True


def _sweep(directory: Path, pattern: str, cutoff: float, remove: Callable[[Path, float], bool],
           stop: Callable[[], bool]) -> int:
    removed = failures = 0
    first_error: OSError | None = None
    with os.scandir(directory) as entries:
        for entry in entries:
            if stop():
                break
            if not fnmatchcase(entry.name, pattern):
                continue
            try:
                removed += int(remove(directory / entry.name, cutoff))
            except FileNotFoundError:
                continue
            except OSError as error:
                failures += 1
                first_error = first_error or error
    if first_error is not None:
        raise OSError(f"{failures} log entries could not be cleaned: {first_error}") from first_error
    return removed
