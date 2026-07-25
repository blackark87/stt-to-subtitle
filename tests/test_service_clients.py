import unittest

from stt_to_subtitle.service_clients import RetryingJSONClient, batch_segments


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
