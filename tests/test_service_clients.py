from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.service_clients import (
    RetryingJSONClient,
    STTAPIClient,
    batch_segments,
)


class BatchSegmentsTests(unittest.TestCase):
    def test_splits_on_count_without_losing_segments(self) -> None:
        segments = [
            {"id": f"segment-{index}", "text": "日本語"}
            for index in range(5)
        ]

        batches = batch_segments(
            segments,
            max_segments=2,
            max_characters=100,
        )

        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        self.assertEqual(
            [item["id"] for batch in batches for item in batch],
            [item["id"] for item in segments],
        )

    def test_single_long_segment_is_kept_intact(self) -> None:
        segments = [{"id": "one", "text": "長" * 20}]

        self.assertEqual(
            batch_segments(
                segments,
                max_segments=30,
                max_characters=10,
            ),
            [segments],
        )


class AuthenticationHeaderTests(unittest.TestCase):
    def test_omits_authorization_header_when_token_is_blank(self) -> None:
        client = RetryingJSONClient(token="")

        self.assertEqual(client.headers, {"Accept": "application/json"})


class STTAPIClientProgressTests(unittest.TestCase):
    def test_forwards_changed_chunk_progress_while_polling(self) -> None:
        running = Mock(status_code=200)
        running.json.return_value = {
            "status": "running",
            "chunk_progress": {
                "created": 20,
                "completed": 10,
                "in_progress": 10,
            },
        }
        completed = Mock(status_code=200)
        completed.json.return_value = {
            "status": "completed",
            "chunk_progress": {
                "created": 23,
                "completed": 23,
                "in_progress": 0,
            },
        }
        result = Mock(status_code=200)
        result.json.return_value = {
            "schema_version": 1,
            "job_id": "remote-job",
            "segments": [],
        }
        client = STTAPIClient("http://stt.test", "", poll_interval=0.01)
        client.request = Mock(side_effect=[running, completed, result])
        progress = []

        with patch("stt_to_subtitle.service_clients.time.sleep"):
            client.transcribe(
                Path("/not-read.wav"),
                options={},
                idempotency_key="key",
                existing_job_id="remote-job",
                on_progress=progress.append,
            )

        self.assertEqual(
            progress,
            [
                {
                    "created": 20,
                    "completed": 10,
                    "in_progress": 10,
                },
                {
                    "created": 23,
                    "completed": 23,
                    "in_progress": 0,
                },
            ],
        )
