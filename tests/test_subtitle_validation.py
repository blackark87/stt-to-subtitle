from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.subtitle_validation import (
    SubtitleCue,
    build_subtitle_validator_payload,
    compare_subtitles,
    discover_external_subtitles,
    parse_subtitle,
    render_webvtt,
    subtitle_asset_hash,
)


class ExternalSubtitleDiscoveryTests(unittest.TestCase):
    def test_discovers_plain_same_stem_sidecars_in_priority_order(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "sample.mp4"
            media.touch()
            (root / "sample.SRT").write_text("srt", encoding="utf-8")
            (root / "sample.vtt").write_text("vtt", encoding="utf-8")
            (root / "sample.ko.srt").write_text("generated", encoding="utf-8")
            (root / "other.ass").write_text("other", encoding="utf-8")

            discovered = discover_external_subtitles(media)

            self.assertEqual(
                [path.name for path in discovered],
                ["sample.vtt", "sample.SRT"],
            )

    def test_asset_hash_changes_when_any_sidecar_changes(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            path.write_text("first", encoding="utf-8")
            first = subtitle_asset_hash((path,))
            path.write_text("second", encoding="utf-8")
            self.assertNotEqual(first, subtitle_asset_hash((path,)))


class SubtitleParsingTests(unittest.TestCase):
    def test_parses_srt_vtt_and_ass(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "sample.srt"
            srt.write_text(
                "1\n00:00:01,000 --> 00:00:02,500\n안녕하세요\n",
                encoding="utf-8",
            )
            vtt = root / "sample.vtt"
            vtt.write_text(
                "WEBVTT\n\n00:00:03.000 --> 00:00:04.000\n반갑습니다\n",
                encoding="utf-8",
            )
            ass = root / "sample.ass"
            ass.write_text(
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:05.00,0:00:06.25,Default,,0,0,0,,"
                "{\\i1}좋아요\\N다음 줄\n",
                encoding="utf-8",
            )

            self.assertEqual(parse_subtitle(srt)[0].text, "안녕하세요")
            self.assertEqual(parse_subtitle(vtt)[0].start, 3.0)
            self.assertEqual(parse_subtitle(ass)[0].text, "좋아요\n다음 줄")

    def test_renders_normalized_webvtt(self) -> None:
        rendered = render_webvtt([SubtitleCue(1, 1.25, 2.5, "한국어")])
        self.assertEqual(
            rendered,
            "WEBVTT\n\n1\n00:00:01.250 --> 00:00:02.500\n한국어\n",
        )


class SubtitleComparisonTests(unittest.TestCase):
    def test_perfect_match_has_full_coverage_and_no_issues(self) -> None:
        cue = SubtitleCue(1, 1.0, 3.0, "같은 한국어 자막")
        result = compare_subtitles([cue], [cue])
        self.assertEqual(result["summary"]["time_coverage"], 1.0)
        self.assertEqual(result["summary"]["average_text_similarity"], 1.0)
        self.assertEqual(result["issues"], [])

    def test_reports_missing_reference_cue(self) -> None:
        result = compare_subtitles(
            [
                SubtitleCue(1, 0.0, 1.0, "첫 문장"),
                SubtitleCue(2, 2.0, 3.0, "두 번째 문장"),
            ],
            [SubtitleCue(1, 0.0, 1.0, "첫 문장")],
        )
        self.assertEqual(result["summary"]["unmatched_reference_cues"], 1)
        self.assertEqual(result["issues"][0]["code"], "missing_candidate")

    def test_builds_bounded_deterministic_llm_payload(self) -> None:
        reference = [
            SubtitleCue(index, float(index), float(index + 1), f"기준 {index}")
            for index in range(1, 140)
        ]
        candidate = [
            SubtitleCue(index, float(index), float(index + 1), f"생성 {index}")
            for index in range(1, 140)
        ]
        metrics = compare_subtitles(reference, candidate)

        payload = build_subtitle_validator_payload(metrics)

        self.assertEqual(len(payload["segments"]), 120)
        self.assertEqual(payload, json.loads(json.dumps(payload)))


if __name__ == "__main__":
    unittest.main()
