# SPDX-License-Identifier: AGPL-3.0-or-later
"""Size and age rotation for the application log.

Rotation runs on the next write, the same way the audit log does. There is
no timer and no background thread. A zero max size means the caller should
use a plain file handler instead.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path


class RotatingSizeAgeHandler(logging.Handler):
    def __init__(self, path: Path, max_bytes: int, retention_days: int) -> None:
        super().__init__()
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero.")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._rotate_if_needed()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
            self._purge_rotated()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream and not self._stream.closed:
                self._stream.close()
        finally:
            super().close()

    def _reopen(self) -> None:
        self._stream = self.path.open("a", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        self._stream.flush()
        if not self.path.exists():
            self._reopen()
            return
        stat = self.path.stat()
        if stat.st_size == 0:
            return
        too_big = stat.st_size >= self.max_bytes
        stale = False
        if self.retention_days > 0:
            mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).date()
            stale = mtime < datetime.now(timezone.utc).date()
        if not too_big and not stale:
            return
        self._stream.close()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = self.path.with_name(f"{self.path.stem}-{stamp}{self.path.suffix}")
        self.path.replace(dest)
        self._reopen()

    def _purge_rotated(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = time.time() - (self.retention_days * 86400)
        pattern = f"{self.path.stem}-*{self.path.suffix}"
        for candidate in self.path.parent.glob(pattern):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue
