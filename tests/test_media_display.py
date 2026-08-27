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

    def test_sorts_and_limits_folders_while_reporting_the_total(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            recent = root / "recent"
            old.mkdir()
            recent.mkdir()
            os.utime(old, (10, 10))
            os.utime(recent, (20, 20))
            library = MediaLibrary(root)

            decorated = decorate_media_listing(
                library,
                library.browse(),
                [],
                folder_sort="modified_desc",
                folder_limit=1,
            )

            self.assertEqual(decorated["folder_total"], 2)
            self.assertEqual(
                [folder["name"] for folder in decorated["folders"]],
                ["recent"],
            )


if __name__ == "__main__":
    unittest.main()
