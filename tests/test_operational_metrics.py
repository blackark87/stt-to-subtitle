import unittest

from stt_to_subtitle.operational_metrics import prometheus_exposition


class PrometheusExpositionTests(unittest.TestCase):
    def test_renders_snapshot_without_remote_job_id_labels(self) -> None:
        output = prometheus_exposition(
            {
                "jobs": {
                    "total": 2,
                    "by_state": {"waiting": 1, "running": 1},
                    "by_phase": {"translation": 2},
                    "by_phase_state": {
                        "translation": {"waiting": 1, "running": 1}
                    },
                    "by_reason": {"lm_unavailable": 1},
                    "oldest_waiting_seconds": {"translation": 12.5},
                    "total_retries": 1,
                    "max_attempt": 2,
                },
                "leases": {"active": 1, "expired_running": 0},
                "database": {
                    "valid": True,
                    "foreign_keys_enabled": True,
                    "foreign_key_violation_count": 0,
                },
                "remote_stt": {
                    "running_job_ids": ["private-remote-id"],
                    "cancel_pending_job_ids": [],
                },
                "dependencies": [],
                "events": {"by_code": {}, "stages": {}},
                "measurements": [
                    {
                        "metric": "external.request.duration_seconds",
                        "labels": {
                            "service": "translation_lm",
                            "outcome": "success",
                        },
                        "sample_count": 2,
                        "total": 1.5,
                        "maximum": 1.0,
                        "last_value": 0.5,
                        "updated_at": 123.0,
                    }
                ],
            }
        )

        self.assertIn(
            'stt_to_subtitle_jobs_by_state{state="waiting"} 1',
            output,
        )
        self.assertIn(
            'stt_to_subtitle_remote_stt_jobs{state="running"} 1',
            output,
        )
        self.assertIn(
            "stt_to_subtitle_operational_measurement_samples_total"
            '{metric="external.request.duration_seconds",'
            'outcome="success",service="translation_lm"} 2',
            output,
        )
        self.assertNotIn("private-remote-id", output)

    def test_escapes_prometheus_label_values(self) -> None:
        output = prometheus_exposition(
            {
                "events": {
                    "by_code": {'line\n"quoted"': 1},
                    "stages": {},
                }
            }
        )

        self.assertIn('code="line\\n\\"quoted\\""', output)

    def test_normalizes_non_prometheus_measurement_label_names(self) -> None:
        output = prometheus_exposition(
            {
                "measurements": [
                    {
                        "metric": "custom.metric",
                        "labels": {"error-type": "timeout"},
                        "sample_count": 1,
                        "total": 1,
                        "maximum": 1,
                        "last_value": 1,
                        "updated_at": 1,
                    }
                ]
            }
        )

        self.assertIn('error_type="timeout"', output)
