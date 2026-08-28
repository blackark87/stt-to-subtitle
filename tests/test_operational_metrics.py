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
                    "migrations": {
                        "applied_count": 5,
                        "latest_sequence": 40,
                        "unsequenced_count": 0,
                    },
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
            "stt_to_subtitle_database_migrations"
            '{statistic="latest_sequence"} 40',
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

    def test_renders_stage_duration_counters_with_runtime_labels(self) -> None:
        output = prometheus_exposition(
            {
                "measurements": [
                    {
                        "metric": "pipeline.stage.duration_seconds",
                        "labels": {
                            "phase": "transcription",
                            "outcome": "completed",
                            "runtime_id": "external-a",
                            "media_duration_bucket_minutes": 15,
                        },
                        "sample_count": 3,
                        "total": 45.5,
                        "maximum": 20.0,
                        "last_value": 15.0,
                        "updated_at": 123.0,
                    },
                    {
                        "metric": "translation.pass.active_seconds",
                        "labels": {
                            "pass": "draft",
                            "outcome": "completed",
                            "media_duration_bucket_minutes": 15,
                        },
                        "sample_count": 2,
                        "total": 30.0,
                        "maximum": 18.0,
                        "last_value": 12.0,
                        "updated_at": 124.0,
                    },
                ]
            }
        )

        labels = (
            '{media_duration_bucket_minutes="15",outcome="completed",'
            'phase="transcription",'
            'runtime_id="external-a"}'
        )
        self.assertIn(
            f"stt_to_subtitle_stage_duration_seconds_count{labels} 3",
            output,
        )
        self.assertIn(
            f"stt_to_subtitle_stage_duration_seconds_sum{labels} 45.5",
            output,
        )
        self.assertIn(
            f"stt_to_subtitle_stage_duration_seconds_maximum{labels} 20",
            output,
        )
        translation_labels = (
            '{media_duration_bucket_minutes="15",outcome="completed",'
            'pass="draft"}'
        )
        self.assertIn(
            "stt_to_subtitle_translation_pass_active_seconds_count"
            f"{translation_labels} 2",
            output,
        )
        self.assertIn(
            "stt_to_subtitle_translation_pass_active_seconds_sum"
            f"{translation_labels} 30",
            output,
        )
