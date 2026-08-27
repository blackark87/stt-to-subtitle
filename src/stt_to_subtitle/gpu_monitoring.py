"""Read current NVIDIA GPU telemetry from a Prometheus HTTP API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import logging
import math
from threading import Lock
import time
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)
GPU_METRICS_QUERY = (
    '{__name__=~"DCGM_FI_DEV_(GPU_UTIL|FB_USED|FB_FREE|FB_TOTAL|GPU_TEMP|POWER_USAGE)"}'
)
METRIC_FIELDS = {
    "DCGM_FI_DEV_GPU_UTIL": "utilization_percent",
    "DCGM_FI_DEV_FB_USED": "memory_used_mib",
    "DCGM_FI_DEV_FB_FREE": "_memory_free_mib",
    "DCGM_FI_DEV_FB_TOTAL": "memory_total_mib",
    "DCGM_FI_DEV_GPU_TEMP": "temperature_celsius",
    "DCGM_FI_DEV_POWER_USAGE": "power_watts",
}


@dataclass(frozen=True)
class GpuDevice:
    """Normalized current metrics for one GPU."""

    id: str
    index: str
    model_name: str
    hostname: str
    utilization_percent: float | None = None
    memory_used_mib: float | None = None
    memory_total_mib: float | None = None
    temperature_celsius: float | None = None
    power_watts: float | None = None

    @property
    def display_name(self) -> str:
        if self.model_name:
            return self.model_name
        if self.index:
            return f"GPU {self.index}"
        return "GPU"

    @property
    def memory_percent(self) -> float | None:
        if (
            self.memory_used_mib is None
            or self.memory_total_mib is None
            or self.memory_total_mib <= 0
        ):
            return None
        return max(
            0.0,
            min(100.0, 100.0 * self.memory_used_mib / self.memory_total_mib),
        )

    @property
    def memory_used_gib(self) -> float | None:
        if self.memory_used_mib is None:
            return None
        return self.memory_used_mib / 1024.0

    @property
    def memory_total_gib(self) -> float | None:
        if self.memory_total_mib is None:
            return None
        return self.memory_total_mib / 1024.0


@dataclass(frozen=True)
class GpuSnapshot:
    """GPU monitoring state rendered by the web dashboard."""

    configured: bool
    available: bool
    devices: tuple[GpuDevice, ...] = ()
    error: str = ""
    error_code: str = ""
    observed_at: float | None = None
    last_success_at: float | None = None
    stale: bool = False


def gpu_snapshot_payload(snapshot: GpuSnapshot) -> dict[str, Any]:
    """Serialize a snapshot with the calculated fields used by web clients."""
    return {
        "configured": snapshot.configured,
        "available": snapshot.available,
        "devices": [
            {
                "id": device.id,
                "index": device.index,
                "model_name": device.model_name,
                "display_name": device.display_name,
                "hostname": device.hostname,
                "utilization_percent": device.utilization_percent,
                "memory_used_mib": device.memory_used_mib,
                "memory_total_mib": device.memory_total_mib,
                "memory_percent": device.memory_percent,
                "memory_used_gib": device.memory_used_gib,
                "memory_total_gib": device.memory_total_gib,
                "temperature_celsius": device.temperature_celsius,
                "power_watts": device.power_watts,
            }
            for device in snapshot.devices
        ],
        "error": snapshot.error,
        "error_code": snapshot.error_code,
        "observed_at": snapshot.observed_at,
        "last_success_at": snapshot.last_success_at,
        "stale": snapshot.stale,
    }


class PrometheusGpuMonitor:
    """Fetch and briefly cache DCGM instant vectors from Prometheus."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str = "",
        timeout_seconds: float = 3.0,
        cache_seconds: float = 10.0,
        session: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.session = session or requests.Session()
        self.clock = clock
        self.wall_clock = wall_clock
        self._lock = Lock()
        self._cached_at = 0.0
        self._cached: GpuSnapshot | None = None
        self._last_success: GpuSnapshot | None = None

    def snapshot(self) -> GpuSnapshot:
        if not self.base_url:
            return GpuSnapshot(configured=False, available=False)

        now = self.clock()
        with self._lock:
            if (
                self._cached is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return self._cached
            try:
                snapshot = self._fetch()
            except (requests.RequestException, TypeError, ValueError) as error:
                LOGGER.warning(
                    "Prometheus GPU metric request failed (%s)",
                    type(error).__name__,
                )
                snapshot = GpuSnapshot(
                    configured=True,
                    available=False,
                    error="Prometheus에서 GPU 메트릭을 가져오지 못했습니다.",
                    error_code=_request_error_code(error),
                    observed_at=self.wall_clock(),
                )
            else:
                snapshot = replace(snapshot, observed_at=self.wall_clock())

            if snapshot.available:
                snapshot = replace(
                    snapshot,
                    last_success_at=snapshot.observed_at,
                )
                self._last_success = snapshot
            elif self._last_success is not None:
                snapshot = replace(
                    snapshot,
                    devices=self._last_success.devices,
                    last_success_at=self._last_success.last_success_at,
                    stale=True,
                )
            self._cached = snapshot
            self._cached_at = now
            return snapshot

    def _fetch(self) -> GpuSnapshot:
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        response = self.session.get(
            f"{self.base_url}/api/v1/query",
            params={"query": GPU_METRICS_QUERY},
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or payload.get("status") != "success":
            raise ValueError("Prometheus returned an unsuccessful response")
        data = payload.get("data")
        if not isinstance(data, Mapping) or data.get("resultType") != "vector":
            raise ValueError("Prometheus GPU query did not return an instant vector")
        results = data.get("result")
        if not isinstance(results, list):
            raise ValueError("Prometheus GPU query result must be a list")
        devices = _normalize_devices(results)
        if not devices:
            return GpuSnapshot(
                configured=True,
                available=False,
                error="Prometheus에 DCGM GPU 메트릭이 없습니다.",
                error_code="no_dcgm_metrics",
            )
        return GpuSnapshot(
            configured=True,
            available=True,
            devices=devices,
        )


def _normalize_devices(results: list[object]) -> tuple[GpuDevice, ...]:
    devices: dict[str, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, Mapping):
            continue
        labels = item.get("metric")
        value_pair = item.get("value")
        if not isinstance(labels, Mapping):
            continue
        if not isinstance(value_pair, list) or len(value_pair) < 2:
            continue
        metric_name = str(labels.get("__name__", ""))
        field = METRIC_FIELDS.get(metric_name)
        if field is None:
            continue
        value = _finite_float(value_pair[1])
        if value is None:
            continue
        index = str(labels.get("gpu", labels.get("device", "")))
        uuid = str(labels.get("UUID", labels.get("uuid", "")))
        device_id = uuid or f"gpu-{index or 'unknown'}"
        device = devices.setdefault(
            device_id,
            {
                "id": device_id,
                "index": index,
                "model_name": str(
                    labels.get("modelName", labels.get("model", ""))
                ),
                "hostname": str(
                    labels.get("hostname", labels.get("Hostname", ""))
                ),
            },
        )
        device[field] = value

    normalized = []
    for device in devices.values():
        memory_free_mib = device.pop("_memory_free_mib", None)
        if (
            "memory_total_mib" not in device
            and "memory_used_mib" in device
            and memory_free_mib is not None
        ):
            device["memory_total_mib"] = (
                device["memory_used_mib"] + memory_free_mib
            )
        normalized.append(GpuDevice(**device))
    normalized.sort(key=lambda device: _gpu_sort_key(device.index, device.id))
    return tuple(normalized)


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _request_error_code(error: Exception) -> str:
    if isinstance(error, requests.Timeout):
        return "timeout"
    if isinstance(error, requests.ConnectionError):
        return "connection_error"
    if isinstance(error, requests.HTTPError):
        return "http_error"
    return "invalid_response"


def _gpu_sort_key(index: str, device_id: str) -> tuple[int, int | str, str]:
    try:
        return (0, int(index), device_id)
    except ValueError:
        return (1, index, device_id)
