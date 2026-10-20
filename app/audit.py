"""Append-only JSON-lines audit log with size and age rotation.

Several processes can append to one file. A lock file beside the data file
serializes rotation and writes. When a fallback path is set and the configured
path cannot be opened, writes go to the fallback and `warning` is set.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.locking import exclusive_lock
from app.redact import redact

log = logging.getLogger("ipad")

AUDIT_FALLBACK_WARNING = (
    "The configured audit log path is unavailable. "
    "This session is writing changes to a local log on this computer instead."
)
_PROBE_SECONDS = 5.0


class AuditLog:
    def __init__(
        self,
        path: Path,
        retention_days: int,
        max_bytes: int,
        fallback_path: Path | None = None,
    ) -> None:
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._fallback_path = fallback_path
        self.warning = ""
        self.path = self._open_path(path)

    def write(
        self,
        *,
        username: str,
        device_id: str,
        serial_number: str,
        action: str,
        before: Any,
        after: Any,
        result: str,
        detail: str = "",
        jamf_host: str = "",
    ) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "username": username,
            "jamfHost": jamf_host,
            "deviceId": device_id,
            "serialNumber": serial_number,
            "action": action,
            "before": before,
            "after": after,
            "result": result,
            "detail": detail,
        }
        line = json.dumps(redact(event), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._append(self.path, line)
            except OSError:
                if self._fallback_path is None or self.path == self._fallback_path:
                    raise
                self._ensure(self._fallback_path)
                self._note_fallback(self.path)
                self.path = self._fallback_path
                self._append(self.path, line)

    def _open_path(self, path: Path) -> Path:
        if self._fallback_path is None:
            self._ensure(path)
            return path
        if self._usable(path):
            return path
        self._ensure(self._fallback_path)
        self._note_fallback(path)
        return self._fallback_path

    def _note_fallback(self, requested: Path) -> None:
        self.warning = AUDIT_FALLBACK_WARNING
        log.warning("Audit log path is unavailable (%s). Writing to %s.", requested, self._fallback_path)

    def _usable(self, path: Path) -> bool:
        box: list[BaseException | None] = [None]

        def attempt() -> None:
            try:
                self._ensure(path)
            except BaseException as exc:  # noqa: BLE001 - probe must not escape the thread
                box[0] = exc

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(_PROBE_SECONDS)
        return not worker.is_alive() and box[0] is None

    def _ensure(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass

    def _append(self, path: Path, line: str) -> None:
        lock_path = path.with_name(path.name + ".lock")
        with exclusive_lock(lock_path):
            self._rotate_if_needed()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            self._purge_rotated()

    def _rotate_if_needed(self) -> None:
        if not self.path.exists():
            return
        stat = self.path.stat()
        if stat.st_size == 0:
            return
        mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).date()
        today = datetime.now(timezone.utc).date()
        if stat.st_size < self.max_bytes and mtime >= today:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = self.path.with_name(f"{self.path.stem}-{stamp}{self.path.suffix}")
        self.path.replace(dest)

    def _purge_rotated(self) -> None:
        cutoff = time.time() - (self.retention_days * 86400)
        pattern = f"{self.path.stem}-*{self.path.suffix}"
        for candidate in self.path.parent.glob(pattern):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue
