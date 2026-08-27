import unittest
from unittest.mock import Mock

import requests

from stt_to_subtitle.gpu_monitoring import (
    GPU_METRICS_QUERY,
    GpuDevice,
    GpuSnapshot,
    PrometheusGpuMonitor,
    gpu_snapshot_payload,
)


def metric(name: str, value: str, **labels: str) -> dict[str, object]:
    return {
        "metric": {"__name__": name, **labels},
        "value": [1720000000, value],
    }


class PrometheusGpuMonitorTests(unittest.TestCase):
    def test_public_payload_includes_calculated_memory_fields(self) -> None:
        payload = gpu_snapshot_payload(
            GpuSnapshot(
                configured=True,
                available=True,
                devices=(
                    GpuDevice(
                        id="GPU-one",
                        index="0",
                        model_name="NVIDIA Test GPU",
                        hostname="runtime",
                        memory_used_mib=3.0,
                        memory_total_mib=9873.0,
                    ),
                ),
            )
        )

        device = payload["devices"][0]
        self.assertEqual(device["display_name"], "NVIDIA Test GPU")
        self.assertAlmostEqual(device["memory_percent"], 100 * 3 / 9873)
        self.assertAlmostEqual(device["memory_used_gib"], 3 / 1024)
        self.assertAlmostEqual(device["memory_total_gib"], 9873 / 1024)

    def test_reports_unconfigured_without_a_request(self) -> None:
        session = Mock()
        snapshot = PrometheusGpuMonitor("", session=session).snapshot()

        self.assertEqual(
            snapshot,
            GpuSnapshot(configured=False, available=False),
        )
        session.get.assert_not_called()

    def test_normalizes_dcgm_metrics_and_sends_optional_token(self) -> None:
        response = Mock()
        response.json.return_value = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    metric(
                        "DCGM_FI_DEV_GPU_UTIL",
                        "73",
                        gpu="0",
                        UUID="GPU-one",
                        modelName="NVIDIA Test GPU",
                    ),
                    metric(
                        "DCGM_FI_DEV_FB_USED",
                        "8192",
                        gpu="0",
                        UUID="GPU-one",
                    ),
                    metric(
                        "DCGM_FI_DEV_FB_TOTAL",
                        "16384",
                        gpu="0",
                        UUID="GPU-one",
                    ),
                    metric(
                        "DCGM_FI_DEV_GPU_TEMP",
                        "67",
                        gpu="0",
                        UUID="GPU-one",
                    ),
                    metric(
                        "DCGM_FI_DEV_POWER_USAGE",
                        "214.5",
                        gpu="0",
                        UUID="GPU-one",
                    ),
                ],
            },
        }
        session = Mock()
        session.get.return_value = response
        monitor = PrometheusGpuMonitor(
            "http://prometheus:9090/",
            bearer_token="secret-token",
            timeout_seconds=2.5,
            session=session,
            wall_clock=Mock(return_value=1720000001.0),
        )

        snapshot = monitor.snapshot()

        self.assertTrue(snapshot.configured)
        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.observed_at, 1720000001.0)
        self.assertEqual(snapshot.last_success_at, 1720000001.0)
        self.assertFalse(snapshot.stale)
        self.assertEqual(len(snapshot.devices), 1)
        device = snapshot.devices[0]
        self.assertEqual(device.display_name, "NVIDIA Test GPU")
        self.assertEqual(device.utilization_percent, 73.0)
        self.assertEqual(device.memory_percent, 50.0)
        self.assertEqual(device.memory_used_gib, 8.0)
        self.assertEqual(device.memory_total_gib, 16.0)
        self.assertEqual(device.temperature_celsius, 67.0)
        self.assertEqual(device.power_watts, 214.5)
        session.get.assert_called_once_with(
            "http://prometheus:9090/api/v1/query",
            params={"query": GPU_METRICS_QUERY},
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer secret-token",
            },
            timeout=2.5,
        )

    def test_caches_failed_requests_without_exposing_details(self) -> None:
        clock = Mock(return_value=100.0)
        session = Mock()
        session.get.side_effect = requests.ConnectionError(
            "http://prometheus:9090 refused token=secret"
        )
        monitor = PrometheusGpuMonitor(
            "http://prometheus:9090",
            cache_seconds=10,
            session=session,
            clock=clock,
            wall_clock=Mock(return_value=1720000002.0),
        )

        first = monitor.snapshot()
        second = monitor.snapshot()

        self.assertFalse(first.available)
        self.assertEqual(first.error_code, "connection_error")
        self.assertEqual(first.observed_at, 1720000002.0)
        self.assertNotIn("secret", first.error)
        self.assertIs(first, second)
        session.get.assert_called_once()

    def test_derives_total_memory_when_dcgm_exposes_free_memory(self) -> None:
        response = Mock()
        response.json.return_value = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    metric(
                        "DCGM_FI_DEV_FB_USED",
                        "3",
                        gpu="0",
                        UUID="GPU-one",
                    ),
                    metric(
                        "DCGM_FI_DEV_FB_FREE",
                        "997",
                        gpu="0",
                        UUID="GPU-one",
                    ),
                ],
            },
        }
        session = Mock()
        session.get.return_value = response

        snapshot = PrometheusGpuMonitor(
            "http://prometheus:9090",
            session=session,
        ).snapshot()

        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.devices[0].memory_used_mib, 3.0)
        self.assertEqual(snapshot.devices[0].memory_total_mib, 1000.0)
        self.assertEqual(snapshot.devices[0].memory_percent, 0.3)

    def test_reports_missing_dcgm_series(self) -> None:
        response = Mock()
        response.json.return_value = {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }
        session = Mock()
        session.get.return_value = response

        snapshot = PrometheusGpuMonitor(
            "http://prometheus:9090",
            session=session,
        ).snapshot()

        self.assertTrue(snapshot.configured)
        self.assertFalse(snapshot.available)
        self.assertIn("DCGM", snapshot.error)
        self.assertEqual(snapshot.error_code, "no_dcgm_metrics")

    def test_preserves_last_successful_devices_when_refresh_fails(self) -> None:
        response = Mock()
        response.json.return_value = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    metric(
                        "DCGM_FI_DEV_GPU_UTIL",
                        "48",
                        gpu="0",
                        UUID="GPU-one",
                    )
                ],
            },
        }
        session = Mock()
        session.get.side_effect = [
            response,
            requests.ConnectionError("prometheus DNS failed"),
        ]
        clock = Mock(side_effect=[100.0, 111.0])
        wall_clock = Mock(side_effect=[1720000000.0, 1720000011.0])
        monitor = PrometheusGpuMonitor(
            "http://prometheus:9090",
            cache_seconds=10,
            session=session,
            clock=clock,
            wall_clock=wall_clock,
        )

        first = monitor.snapshot()
        stale = monitor.snapshot()

        self.assertTrue(first.available)
        self.assertFalse(stale.available)
        self.assertTrue(stale.stale)
        self.assertEqual(stale.error_code, "connection_error")
        self.assertEqual(stale.devices, first.devices)
        self.assertEqual(stale.last_success_at, first.observed_at)
        self.assertEqual(stale.observed_at, 1720000011.0)


if __name__ == "__main__":
    unittest.main()
