from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.subtitle import (
    build_subtitle_timeline,
    format_srt_timestamp,
    render_ass,
    render_srt,
    render_webvtt,
    write_srt_atomic,
    write_styled_subtitles_atomic,
)


class SubtitleTests(unittest.TestCase):
    def test_renders_korean_text_with_speaker_color_without_label(self) -> None:
        rendered = render_srt(
            [
                {
                    "id": "segment-000001",
                    "start": 3661.234,
                    "end": 3662.5,
                    "speaker": "SPEAKER_00",
                    "text": "こんにちは",
                }
            ],
            [{"id": "segment-000001", "text": "안녕하세요"}],
        )

        self.assertIn("01:01:01,234 --> 01:01:02,500", rendered)
        self.assertIn("안녕하세요", rendered)
        self.assertNotIn("SPEAKER_00", rendered)
        self.assertNotIn("화자 1", rendered)
        self.assertIn('<font color="#67E8F9">', rendered)

    def test_splits_real_speaker_overlap_into_non_overlapping_cues(self) -> None:
        segments = [
            {
                "id": "segment-000001",
                "start": 5.8,
                "end": 8.0,
                "speaker": "SPEAKER_00",
                "text": "大丈夫ですか",
            },
            {
                "id": "segment-000002",
                "start": 7.0,
                "end": 9.2,
                "speaker": "SPEAKER_01",
                "text": "はい",
            },
        ]
        translations = [
            {"id": "segment-000001", "text": "괜찮아요?"},
            {"id": "segment-000002", "text": "네, 괜찮습니다."},
        ]

        timeline = build_subtitle_timeline(segments, translations)

        self.assertEqual(
            [(cue.start, cue.end) for cue in timeline.cues],
            [(5.8, 7.0), (7.0, 8.0), (8.0, 9.2)],
        )
        self.assertEqual(
            [len(cue.lines) for cue in timeline.cues],
            [1, 2, 1],
        )
        rendered = render_srt(segments, translations)
        self.assertNotIn("화자 1", rendered)
        self.assertNotIn("화자 2", rendered)
        self.assertIn('<font color="#67E8F9">괜찮아요?</font>', rendered)
        self.assertIn(
            '<font color="#FDE047">네, 괜찮습니다.</font>',
            rendered,
        )

    def test_replaces_overlapping_lines_from_the_same_speaker(self) -> None:
        timeline = build_subtitle_timeline(
            [
                {
                    "id": "segment-000001",
                    "start": 1.0,
                    "end": 4.0,
                    "speaker": "SPEAKER_00",
                    "text": "一",
                },
                {
                    "id": "segment-000002",
                    "start": 3.0,
                    "end": 5.0,
                    "speaker": "SPEAKER_00",
                    "text": "二",
                },
            ],
            [
                {"id": "segment-000001", "text": "첫 번째"},
                {"id": "segment-000002", "text": "두 번째"},
            ],
        )

        self.assertEqual(
            [
                (cue.start, cue.end, [line.text for line in cue.lines])
                for cue in timeline.cues
            ],
            [
                (1.0, 3.0, ["첫 번째"]),
                (3.0, 5.0, ["두 번째"]),
            ],
        )

    def test_repairs_legacy_abnormally_long_timestamp(self) -> None:
        timeline = build_subtitle_timeline(
            [
                {
                    "id": "segment-000001",
                    "start": 5.8,
                    "end": 55.0,
                    "speaker": "SPEAKER_00",
                    "text": "長い終了時刻",
                },
                {
                    "id": "segment-000002",
                    "start": 9.2,
                    "end": 10.0,
                    "speaker": "SPEAKER_00",
                    "text": "次",
                },
            ],
            [
                {"id": "segment-000001", "text": "잘못된 긴 종료 시각"},
                {"id": "segment-000002", "text": "다음"},
            ],
        )

        self.assertEqual(
            timeline.repaired_segment_ids,
            ("segment-000001",),
        )
        self.assertEqual(timeline.cues[0].end, 9.2)

    def test_legacy_repair_does_not_cut_at_another_speaker_start(self) -> None:
        timeline = build_subtitle_timeline(
            [
                {
                    "id": "segment-000001",
                    "start": 5.8,
                    "end": 55.0,
                    "speaker": "SPEAKER_00",
                    "text": "長い終了時刻",
                },
                {
                    "id": "segment-000002",
                    "start": 7.0,
                    "end": 8.0,
                    "speaker": "SPEAKER_01",
                    "text": "重なる話者",
                },
            ],
            [
                {"id": "segment-000001", "text": "잘못된 긴 종료 시각"},
                {"id": "segment-000002", "text": "겹치는 다른 화자"},
            ],
        )

        overlap = next(
            cue
            for cue in timeline.cues
            if cue.start == 7.0
        )
        self.assertEqual(len(overlap.lines), 2)
        self.assertGreater(timeline.cues[-1].end, 7.0)

    def test_renders_ass_and_webvtt_speaker_styles(self) -> None:
        segments = [
            {
                "id": "segment-000001",
                "start": 1.0,
                "end": 2.0,
                "speaker": "SPEAKER_00",
                "text": "こんにちは",
            }
        ]
        translations = [{"id": "segment-000001", "text": "안녕하세요"}]

        ass = render_ass(segments, translations)
        webvtt = render_webvtt(segments, translations)

        self.assertIn("[V4+ Styles]", ass)
        self.assertIn("Noto Sans CJK KR", ass)
        self.assertIn(r"{\c&H00F9E867&}안녕하세요", ass)
        self.assertIn("<c.speaker-1>안녕하세요</c>", webvtt)
        self.assertNotIn("화자 1", ass)
        self.assertNotIn("화자 1", webvtt)

    def test_refuses_existing_srt_without_force(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ko.srt"
            path.write_text("existing", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                write_srt_atomic(path, [], [], overwrite=False)

            self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_refuses_existing_ass_before_writing_srt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            srt_path = root / "sample.ko.srt"
            ass_path = root / "sample.ko.ass"
            ass_path.write_text("existing", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                write_styled_subtitles_atomic(
                    srt_path,
                    ass_path,
                    [],
                    [],
                    overwrite=False,
                )

            self.assertFalse(srt_path.exists())
            self.assertEqual(
                ass_path.read_text(encoding="utf-8"),
                "existing",
            )

    def test_timestamp_never_becomes_negative(self) -> None:
        self.assertEqual(format_srt_timestamp(-1), "00:00:00,000")
