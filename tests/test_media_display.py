import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.backend_config import MediaLibrary
from stt_to_subtitle.media_display import (
    decorate_media_listing,
    flatten_media_display_folders,
)
from stt_to_subtitle.path_display import PathDisplayRule


class MediaDisplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = PathDisplayRule(
            id="display-rule",
            source_pattern="av/japan/{actress}/{content_id}/{filename}",
            display_pattern="av/japan/{actress}/{filename}",
            created_at=0,
            updated_at=0,
        )

    def test_lifts_shortened_media_without_changing_source_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            title = root / "av" / "japan" / "Actor" / "ABC-001"
            title.mkdir(parents=True)
            (title / "ABC-001.mp4").write_bytes(b"media")
            library = MediaLibrary(root)

            listing = flatten_media_display_folders(
                library,
                library.browse("av/japan/Actor"),
                [self.rule],
            )
            decorated = decorate_media_listing(library, listing, [self.rule])

            self.assertEqual(decorated["folders"], [])
            self.assertEqual(
                decorated["files"][0]["path"],
                "av/japan/Actor/ABC-001/ABC-001.mp4",
            )
            self.assertEqual(
                decorated["files"][0]["display_path"],
                "av/japan/Actor/ABC-001.mp4",
            )

    def test_decorates_actor_folder_with_profile_image(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            actor = root / "av" / "japan" / "Actor"
            (actor / ".actors").mkdir(parents=True)
            (actor / ".actors" / "Actor.jpg").write_bytes(b"profile")
            (actor / "movie.mp4").write_bytes(b"media")
            library = MediaLibrary(root)

            decorated = decorate_media_listing(
                library,
                library.browse("av/japan"),
                [self.rule],
            )

            self.assertEqual(
                decorated["folders"][0]["actor_image_path"],
                "av/japan/Actor/.actors/Actor.jpg",
            )

    def test_sorts_and_pages_folders_while_reporting_the_total(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            middle = root / "middle"
            recent = root / "recent"
            old.mkdir()
            middle.mkdir()
            recent.mkdir()
            os.utime(old, (10, 10))
            os.utime(middle, (15, 15))
            os.utime(recent, (20, 20))
            library = MediaLibrary(root)

            decorated = decorate_media_listing(
                library,
                library.browse(),
                [],
                folder_sort="modified_desc",
                folder_offset=1,
                folder_limit=1,
            )

            self.assertEqual(decorated["folder_total"], 3)
            self.assertEqual(decorated["folder_offset"], 1)
            self.assertEqual(decorated["folder_limit"], 1)
            self.assertEqual(
                [folder["name"] for folder in decorated["folders"]],
                ["middle"],
            )

    def test_sorts_files_by_file_and_nfo_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            library = MediaLibrary(Path(directory))
            listing = {
                "folders": [],
                "files": [
                    {
                        "path": "c.mkv",
                        "name": "c.mkv",
                        "created_at": 10,
                        "modified_at": 30,
                        "nfo_title": "두 번째",
                        "nfo_release_date": "2023-01-02",
                    },
                    {
                        "path": "a.mkv",
                        "name": "a.mkv",
                        "created_at": 30,
                        "modified_at": 10,
                        "nfo_title": "첫 번째",
                        "nfo_release_date": "2024/03/04",
                    },
                    {
                        "path": "b.mkv",
                        "name": "b.mkv",
                        "created_at": 20,
                        "modified_at": 20,
                        "nfo_title": None,
                        "nfo_release_date": None,
                    },
                ],
            }

            expected = {
                "filename": ["a.mkv", "b.mkv", "c.mkv"],
                "created_desc": ["a.mkv", "b.mkv", "c.mkv"],
                "modified_desc": ["c.mkv", "b.mkv", "a.mkv"],
                "nfo_title": ["c.mkv", "a.mkv", "b.mkv"],
                "nfo_release_desc": ["a.mkv", "c.mkv", "b.mkv"],
            }
            for file_sort, names in expected.items():
                with self.subTest(file_sort=file_sort):
                    decorated = decorate_media_listing(
                        library,
                        listing,
                        [],
                        file_sort=file_sort,
                    )
                    self.assertEqual(
                        [item["name"] for item in decorated["files"]],
                        names,
                    )

    def test_rejects_unknown_file_sort(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "media file sort"):
                decorate_media_listing(
                    MediaLibrary(Path(directory)),
                    {"folders": [], "files": []},
                    [],
                    file_sort="unknown",
                )


if __name__ == "__main__":
    unittest.main()
