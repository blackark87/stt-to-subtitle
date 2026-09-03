"""Process-local capacity control shared by GPU-backed service clients."""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from typing import Iterator, Mapping


class ResourceGroupTimeout(RuntimeError):
    """Raised when a resource-group slot cannot be reserved in time."""


class ResourceGroupLimiter:
    """Coordinate request slots across transcription and translation clients."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._capacities: dict[str, int] = {}
        self._active: dict[str, int] = {}

    def configure(self, capacities: Mapping[str, int]) -> None:
        normalized = {
            str(group_id): max(1, int(capacity))
            for group_id, capacity in capacities.items()
            if str(group_id).strip()
        }
        with self._condition:
            self._capacities = normalized
            self._condition.notify_all()

    def capacity(self, group_id: str) -> int:
        with self._condition:
            return self._capacities.get(group_id, 1)

    def active(self, group_id: str) -> int:
        with self._condition:
            return self._active.get(group_id, 0)

    def try_acquire(self, group_id: str) -> bool:
        with self._condition:
            if self._active.get(group_id, 0) >= self.capacity(group_id):
                return False
            self._active[group_id] = self._active.get(group_id, 0) + 1
            return True

    def release(self, group_id: str) -> None:
        with self._condition:
            active = self._active.get(group_id, 0)
            if active <= 1:
                self._active.pop(group_id, None)
            else:
                self._active[group_id] = active - 1
            self._condition.notify_all()

    @contextmanager
    def reserve(self, group_id: str, *, timeout: float) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self.try_acquire(group_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResourceGroupTimeout(
                        f"resource group '{group_id}' capacity wait timed out"
                    )
                self._condition.wait(timeout=min(1.0, remaining))
        try:
            yield
        finally:
            self.release(group_id)
