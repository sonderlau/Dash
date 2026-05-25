from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    from common import write_json
except ModuleNotFoundError:  # pragma: no cover - local package-style invocation
    from scripts.common import write_json


class SnapshotWriter:
    """Thread-safe debounced writer for the daily snapshot JSON.

    Multiple workers may mark the snapshot dirty concurrently. The writer
    flushes to disk at most every `min_interval_seconds` OR every `every_n`
    marks, plus a final forced flush at close time. The on-disk file therefore
    lags the in-memory payload by at most one debounce window — acceptable
    because PDFs and LLM responses are cached upstream and a crash only
    forces re-assembly of the snapshot, not re-payment for tokens.
    """

    def __init__(
        self,
        target: Path,
        payload: dict[str, Any],
        *,
        pretty: bool,
        every_n: int = 5,
        min_interval_seconds: float = 10.0,
        on_flush: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._target = target
        self._payload = payload
        self._pretty = pretty
        self._every_n = max(1, every_n)
        self._min_interval = max(0.0, min_interval_seconds)
        self._on_flush = on_flush

        self._lock = threading.Lock()
        self._dirty_count = 0
        self._last_flush_at = 0.0
        self._closed = False

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload

    def mark_dirty(self) -> bool:
        """Mark the payload dirty; flush if debounce window elapsed.

        Returns True if a flush happened on this call.
        """
        with self._lock:
            if self._closed:
                return False
            self._dirty_count += 1
            now = time.monotonic()
            elapsed = now - self._last_flush_at
            if self._dirty_count >= self._every_n or elapsed >= self._min_interval:
                self._flush_locked(now)
                return True
            return False

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._dirty_count == 0:
                return
            self._flush_locked(time.monotonic())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._dirty_count > 0:
                self._flush_locked(time.monotonic())
            self._closed = True

    def __enter__(self) -> "SnapshotWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _flush_locked(self, now: float) -> None:
        if self._on_flush is not None:
            self._on_flush(self._payload)
        write_json(self._target, self._payload, pretty=self._pretty)
        self._dirty_count = 0
        self._last_flush_at = now
