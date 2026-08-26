import unittest

from stt_to_subtitle.path_display import (
    PathDisplayRule,
    normalize_path_display_patterns,
    shorten_display_path,
)


class PathDisplayTests(unittest.TestCase):
    def rule(self, source: str, display: str) -> PathDisplayRule:
        return PathDisplayRule(
            id="rule",
            source_pattern=source,
            display_pattern=display,
            created_at=0,
            updated_at=0,
        )

    def test_shortens_matching_path_without_changing_filename(self) -> None:
        rule = self.rule(
            "av/japan/{actress}/{content_id}/{filename}",
            "av/japan/{actress}/{filename}",
        )

        self.assertEqual(
            shorten_display_path(
                "/media/av/japan/배우/ABC-001/ABC-001.mp4",
                [rule],
            ),
            "/media/av/japan/배우/ABC-001.mp4",
        )
        self.assertEqual(
            shorten_display_path("Shows/Season 1/episode.mkv", [rule]),
            "Shows/Season 1/episode.mkv",
        )

    def test_supports_literals_and_repeated_placeholders(self) -> None:
        rule = self.rule(
            "{actress}/{content_id}/{content_id}.mp4",
            "{actress}/{content_id}.mp4",
        )

        self.assertEqual(
            shorten_display_path("배우/ABC-001/ABC-001.mp4", [rule]),
            "배우/ABC-001.mp4",
        )
        self.assertEqual(
            shorten_display_path("배우/ABC-001/other.mp4", [rule]),
            "배우/ABC-001/other.mp4",
        )

    def test_rejects_unknown_output_placeholder(self) -> None:
        with self.assertRaisesRegex(ValueError, "원본에 없는 변수"):
            normalize_path_display_patterns(
                "{actress}/{filename}",
                "{unknown}/{filename}",
            )
