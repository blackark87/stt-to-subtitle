"""Tests for shared transcription-stage progress validation."""

from __future__ import annotations

import unittest

from stt_to_subtitle.transcription_progress import validate_stage_progress


class TranscriptionProgressTests(unittest.TestCase):
    def test_source_separation_is_a_supported_stage(self) -> None:
        self.assertEqual(
            validate_stage_progress("source_separation", 1, 7),
            ("source_separation", 1, 7),
        )

if __name__ == "__main__":
    unittest.main()
