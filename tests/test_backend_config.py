import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stt_to_subtitle.backend_config import (
    BackendSettings,
    group_multipart_media,
    MediaLibrary,
    RemoteServerSettings,
    SubtitleValidatorSettings,
    probe_media_duration,
)


class MediaLibraryTests(unittest.TestCase):
    def test_describes_and_resolves_external_subtitles(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "movie.mp4"
            media.write_bytes(b"media")
            subtitle = root / "movie.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n한국어\n",
                encoding="utf-8",
            )
            (root / "movie.ko.srt").write_text("generated", encoding="utf-8")
            library = MediaLibrary(root)

            entry = library.browse()["files"][0]

            self.assertTrue(entry["has_external_subtitle"])
            self.assertEqual(entry["external_subtitle_formats"], ["srt"])
            self.assertEqual(library.external_subtitles("movie.mp4"), (subtitle,))

    def test_recursively_lists_selected_folders_without_metadata_or_links(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Series" / "Season 1"
            season.mkdir(parents=True)
            (root / "Series" / "root.mkv").write_bytes(b"media")
            (season / "episode.mp4").write_bytes(b"media")
            metadata = root / "Series" / "@eaDir"
            metadata.mkdir()
            (metadata / "ignored.mp4").write_bytes(b"media")
            (season / "episode-trailer.mp4").write_bytes(b"media")
            outside = root / "Outside"
            outside.mkdir()
            (outside / "outside.mkv").write_bytes(b"media")
            try:
                (root / "Series" / "outside-link").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError:
                pass

            files = MediaLibrary(root).list_media_recursive(["Series"])

            self.assertEqual(
                files,
                ["Series/root.mkv", "Series/Season 1/episode.mp4"],
            )

    def test_recursive_listing_rejects_an_over_limit_selection(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            (folder / "one.mkv").write_bytes(b"media")
            (folder / "two.mkv").write_bytes(b"media")

            with self.assertRaisesRegex(ValueError, "초과"):
                MediaLibrary(root, maximum_files=1).list_media_recursive(
                    ["folder"]
                )

    def test_browses_only_the_entered_folder_without_recursive_scan(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Series" / "Season 1"
            season.mkdir(parents=True)
            (root / "root-video.mp4").write_bytes(b"media")
            (season / "episode-01.mkv").write_bytes(b"media")
            (season / "episode-01.ko.srt").write_text(
                "subtitle",
                encoding="utf-8",
            )
            (season / "episode-02.mkv").write_bytes(b"media")

            library = MediaLibrary(root)
            with patch("stt_to_subtitle.backend_config.os.walk") as walk:
                root_view = library.browse()
            walk.assert_not_called()
            series_view = library.browse("Series")
            season_view = library.browse("Series/Season 1")

            self.assertEqual(
                [folder["name"] for folder in root_view["folders"]],
                ["Series"],
            )
            self.assertNotIn("video_count", root_view["folders"][0])
            self.assertIsInstance(root_view["folders"][0]["modified_at"], float)
            self.assertEqual(
                [file["name"] for file in root_view["files"]],
                ["root-video.mp4"],
            )
            self.assertEqual(
                [folder["name"] for folder in series_view["folders"]],
                ["Season 1"],
            )
            self.assertEqual(
                [item["name"] for item in series_view["breadcrumbs"]],
                ["미디어 루트", "Series"],
            )
            self.assertEqual(series_view["parent_folder"], "")
            self.assertEqual(
                [file["name"] for file in season_view["files"]],
                ["episode-01.mkv", "episode-02.mkv"],
            )
            self.assertTrue(season_view["files"][0]["has_subtitle"])

    def test_nfo_actor_names_are_exposed_for_media_entries(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested.mkv").write_bytes(b"media")
            (root / "nested.nfo").write_text(
                "<movie><title>제목</title>"
                "<actor><name>미야시타 레나</name><role>본인</role></actor>"
                "<actor><name>사토 아이</name></actor>"
                "<actor><name>미야시타 레나</name></actor>"
                "</movie>",
                encoding="utf-8",
            )
            (root / "plain.mkv").write_bytes(b"media")
            (root / "plain.nfo").write_text(
                "<movie><title>제목</title><actor>모리 히나코</actor></movie>",
                encoding="utf-8",
            )
            (root / "none.mkv").write_bytes(b"media")

            files = {
                item["name"]: item
                for item in MediaLibrary(root).browse()["files"]
            }

            self.assertEqual(
                files["nested.mkv"]["actors"],
                ["미야시타 레나", "사토 아이"],
            )
            self.assertEqual(files["plain.mkv"]["actors"], ["모리 히나코"])
            self.assertEqual(files["none.mkv"]["actors"], [])

    def test_filters_media_recursively_by_nfo_actor_and_title(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Series" / "Season 1"
            season.mkdir(parents=True)
            for filename, title, actor in (
                ("first.mkv", "첫 번째", "미야시타 레나"),
                ("second.mkv", "두 번째", "사토 아이"),
                ("third.mkv", "세 번째", "미야시타 레나"),
            ):
                media = season / filename
                media.write_bytes(b"media")
                media.with_suffix(".nfo").write_text(
                    f"<movie><title>{title}</title>"
                    f"<actor><name>{actor}</name></actor></movie>",
                    encoding="utf-8",
                )

            actor_results = MediaLibrary(root).search_media(
                actor_query="미야시타",
                relative_directory="Series",
            )
            combined_results = MediaLibrary(root).search_media(
                title_query="세 번째",
                actor_query="미야시타 레나",
                relative_directory="Series",
            )

            self.assertEqual(
                [item["name"] for item in actor_results["files"]],
                ["first.mkv", "third.mkv"],
            )
            self.assertEqual(
                [item["name"] for item in combined_results["files"]],
                ["third.mkv"],
            )

    def test_multipart_group_merges_actor_names_without_duplicates(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            show = root / "show"
            show.mkdir()
            for part, actor in ((1, "사토 아이"), (2, "모리 히나코")):
                (show / f"movie-pt{part}.mkv").write_bytes(b"media")
                (show / f"movie-pt{part}.nfo").write_text(
                    "<movie><title>같은 제목</title>"
                    "<actor><name>미야시타 레나</name></actor>"
                    f"<actor><name>{actor}</name></actor>"
                    "</movie>",
                    encoding="utf-8",
                )

            view = MediaLibrary(root).browse("show")
            grouped = group_multipart_media(view["files"])[0]

            self.assertEqual(
                grouped["actors"],
                ["미야시타 레나", "사토 아이", "모리 히나코"],
            )

    def test_multipart_parts_inherit_the_group_level_nfo(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            show = root / "show"
            show.mkdir()
            for part in (1, 2):
                (show / f"movie-pt{part}.mkv").write_bytes(b"media")
            (show / "movie.nfo").write_text(
                "<movie><title>그룹 제목</title></movie>",
                encoding="utf-8",
            )

            view = MediaLibrary(root).browse("show")
            grouped = group_multipart_media(view["files"])[0]

            self.assertTrue(grouped["multipart"])
            self.assertTrue(grouped["has_nfo"])
            self.assertEqual(grouped["title"], "그룹 제목")

    def test_part_level_nfo_takes_precedence_over_the_group_nfo(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            show = root / "show"
            show.mkdir()
            for part in (1, 2):
                (show / f"movie-pt{part}.mkv").write_bytes(b"media")
                (show / f"movie-pt{part}.nfo").write_text(
                    "<movie><title>파트 제목</title></movie>",
                    encoding="utf-8",
                )
            (show / "movie.nfo").write_text(
                "<movie><title>그룹 제목</title></movie>",
                encoding="utf-8",
            )

            view = MediaLibrary(root).browse("show")
            grouped = group_multipart_media(view["files"])[0]

            self.assertEqual(grouped["title"], "파트 제목")

    def test_multipart_without_any_nfo_falls_back_to_the_base_stem(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            show = root / "show"
            show.mkdir()
            for part in (1, 2):
                (show / f"movie-pt{part}.mkv").write_bytes(b"media")

            view = MediaLibrary(root).browse("show")
            grouped = group_multipart_media(view["files"])[0]

            self.assertFalse(grouped["has_nfo"])
            self.assertEqual(grouped["title"], "movie")

    def test_groups_multipart_files_by_sibling_prefix_in_natural_order(
        self,
    ) -> None:
        media_files = [
            {
                "path": "show/movie-pt10.mkv",
                "name": "movie-pt10.mkv",
                "title": "movie-pt10",
                "size": 10,
                "duration_seconds": 10.0,
                "has_subtitle": False,
                "has_nfo": False,
                "poster_path": None,
            },
            {
                "path": "show/movie-pt2.mp4",
                "name": "movie-pt2.mp4",
                "title": "movie-pt2",
                "size": 2,
                "duration_seconds": 2.0,
                "has_subtitle": True,
                "has_nfo": False,
                "poster_path": None,
            },
            {
                "path": "show/movie-pt1.mkv",
                "name": "movie-pt1.mkv",
                "title": "movie-pt1",
                "size": 1,
                "duration_seconds": 1.0,
                "has_subtitle": True,
                "has_nfo": False,
                "poster_path": None,
            },
            {
                "path": "other/movie-pt3.mkv",
                "name": "movie-pt3.mkv",
                "title": "movie-pt3",
                "size": 3,
                "duration_seconds": 3.0,
                "has_subtitle": False,
                "has_nfo": False,
                "poster_path": None,
            },
        ]

        grouped = group_multipart_media(media_files)

        self.assertEqual(len(grouped), 2)
        multipart = grouped[0]
        self.assertTrue(multipart["multipart"])
        self.assertEqual(multipart["name"], "movie")
        self.assertEqual(multipart["path"], "show/movie")
        self.assertEqual(multipart["title"], "movie")
        self.assertEqual(multipart["part_count"], 3)
        self.assertEqual(
            multipart["paths"],
            [
                "show/movie-pt1.mkv",
                "show/movie-pt2.mp4",
                "show/movie-pt10.mkv",
            ],
        )
        self.assertEqual(multipart["size"], 13)
        self.assertEqual(multipart["duration_seconds"], 13.0)
        self.assertFalse(multipart["has_subtitle"])
        self.assertNotIn("multipart", grouped[1])

    def test_searches_display_titles_recursively_within_selected_folder(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Series" / "Season 1"
            season.mkdir(parents=True)
            titled = season / "unrelated-name.mkv"
            titled.write_bytes(b"media")
            titled.with_suffix(".nfo").write_text(
                "<episodedetails><title>첫 번째 에피소드</title></episodedetails>",
                encoding="utf-8",
            )
            (season / "Second Episode.mp4").write_bytes(b"media")
            (root / "Outside Match.mp4").write_bytes(b"media")

            library = MediaLibrary(root)
            nfo_results = library.search_by_title("첫 번째", "Series")
            filename_results = library.search_by_title("second", "Series")
            filename_only = library.search_by_title("unrelated", "Series")

            self.assertEqual(nfo_results["folders"], [])
            self.assertEqual(
                [item["path"] for item in nfo_results["files"]],
                ["Series/Season 1/unrelated-name.mkv"],
            )
            self.assertEqual(
                [item["path"] for item in filename_results["files"]],
                ["Series/Season 1/Second Episode.mp4"],
            )
            self.assertEqual(filename_only["files"], [])
            self.assertEqual(nfo_results["current_folder"], "Series")

    def test_title_search_describes_only_matching_media(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "match.mp4").write_bytes(b"media")
            (root / "other.mp4").write_bytes(b"media")
            probed_paths: list[Path] = []
            library = MediaLibrary(
                root,
                duration_probe=lambda path: probed_paths.append(path) or 1.0,
            )

            results = library.search_by_title("match")

            self.assertEqual(
                [item["name"] for item in results["files"]],
                ["match.mp4"],
            )
            self.assertEqual(probed_paths, [(root / "match.mp4").resolve()])

    def test_excludes_synology_and_desktop_metadata_entries(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            visible = root / "Visible"
            visible.mkdir()
            (visible / "movie.mkv").write_bytes(b"media")
            metadata = root / "@eaDir" / "Visible"
            metadata.mkdir(parents=True)
            (metadata / "thumbnail.mp4").write_bytes(b"metadata")
            recycle = root / "#recycle"
            recycle.mkdir()
            (recycle / "deleted.mkv").write_bytes(b"deleted")
            (root / ".DS_Store").write_bytes(b"metadata")

            library = MediaLibrary(root)
            browser = library.browse()

            self.assertEqual(
                [folder["name"] for folder in browser["folders"]],
                ["Visible"],
            )
            visible_view = library.browse("Visible")
            self.assertEqual(
                [file["name"] for file in visible_view["files"]],
                ["movie.mkv"],
            )
            with self.assertRaisesRegex(ValueError, "metadata"):
                library.browse("@eaDir")
            with self.assertRaisesRegex(ValueError, "metadata"):
                library.resolve_file("@eaDir/Visible/thumbnail.mp4")

    def test_excludes_artwork_sidecar_directories(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            show = root / "show"
            show.mkdir()
            (show / "movie.mkv").write_bytes(b"media")
            for name in ("ExtraFanart", ".actors"):
                sidecar = show / name
                sidecar.mkdir()
                (sidecar / "art.mp4").write_bytes(b"artwork")

            library = MediaLibrary(root)
            view = library.browse("show")

            self.assertEqual(view["folders"], [])
            self.assertEqual(
                [file["name"] for file in view["files"]],
                ["movie.mkv"],
            )
            self.assertEqual(
                library.list_media_recursive(["show"]),
                ["show/movie.mkv"],
            )
            with self.assertRaisesRegex(ValueError, "metadata"):
                library.browse("show/ExtraFanart")
            with self.assertRaisesRegex(ValueError, "metadata"):
                library.browse("show/.actors")

    def test_hides_trailer_mp4_files_from_listing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "movie.mp4",
                "movie-trailer.mp4",
                "MOVIE-TRAILER.MP4",
                "movie-trailer.mkv",
                "movie-trailer-cut.mp4",
            ):
                (root / name).write_bytes(b"media")

            files = MediaLibrary(root).browse()["files"]

            self.assertEqual(
                [file["name"] for file in files],
                [
                    "movie-trailer-cut.mp4",
                    "movie-trailer.mkv",
                    "movie.mp4",
                ],
            )

    def test_probes_and_caches_media_duration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "movie.mkv"
            media.write_bytes(b"media")
            probed_paths: list[Path] = []

            def probe(path: Path) -> float:
                probed_paths.append(path)
                return 6180.4

            library = MediaLibrary(root, duration_probe=probe)

            first = library.browse()["files"][0]
            second = library.browse()["files"][0]

            self.assertEqual(first["duration_seconds"], 6180.4)
            self.assertEqual(second["duration_seconds"], 6180.4)
            self.assertEqual(probed_paths, [media.resolve()])

    def test_ffprobe_duration_failure_does_not_break_listing(self) -> None:
        with TemporaryDirectory() as directory:
            media = Path(directory) / "movie.mkv"
            media.write_bytes(b"media")
            completed = SimpleNamespace(
                returncode=1,
                stdout="",
            )

            with patch(
                "stt_to_subtitle.backend_config.subprocess.run",
                return_value=completed,
            ):
                duration = probe_media_duration(media)

            self.assertIsNone(duration)

    def test_reads_duration_from_ffprobe_output(self) -> None:
        with TemporaryDirectory() as directory:
            media = Path(directory) / "movie.mkv"
            media.write_bytes(b"media")
            completed = SimpleNamespace(
                returncode=0,
                stdout="6180.4\n",
            )

            with patch(
                "stt_to_subtitle.backend_config.subprocess.run",
                return_value=completed,
            ) as run:
                duration = probe_media_duration(media)

            self.assertEqual(duration, 6180.4)
            self.assertEqual(run.call_args.args[0][-1], str(media))

    def test_lists_supported_media_and_detects_subtitle(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "folder" / "movie.mkv"
            media.parent.mkdir()
            media.write_bytes(b"media")
            media.with_name("movie.ko.srt").write_text("subtitle")

            files = MediaLibrary(root).browse("folder")["files"]

            self.assertEqual(files[0]["path"], "folder/movie.mkv")
            self.assertTrue(files[0]["has_subtitle"])
            self.assertFalse(files[0]["has_nfo"])
            self.assertIsNone(files[0]["poster_path"])
            self.assertNotIn("directory", files[0])

    def test_ass_file_alone_marks_media_as_subtitled(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.mkv").write_bytes(b"media")
            (root / "sample.ko.ass").write_text(
                "[Script Info]\n",
                encoding="utf-8",
            )

            files = MediaLibrary(root).browse()["files"]

            self.assertEqual(len(files), 1)
            self.assertTrue(files[0]["has_subtitle"])

    def test_reads_nfo_title_and_local_poster(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            (folder / "movie.mkv").write_bytes(b"media")
            (folder / "movie.nfo").write_text(
                """
                <movie>
                  <title>테스트 영화</title>
                  <thumb aspect="poster">art/movie-poster.jpg</thumb>
                </movie>
                """,
                encoding="utf-8",
            )
            poster = folder / "art" / "movie-poster.jpg"
            poster.parent.mkdir()
            poster.write_bytes(b"poster")

            library = MediaLibrary(root)
            files = library.browse("folder")["files"]

            self.assertTrue(files[0]["has_nfo"])
            self.assertEqual(files[0]["title"], "테스트 영화")
            self.assertEqual(
                files[0]["poster_path"],
                "folder/art/movie-poster.jpg",
            )
            self.assertEqual(
                library.resolve_poster("folder/art/movie-poster.jpg"),
                poster.resolve(),
            )

    def test_finds_actor_profile_in_actor_or_title_metadata_folder(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            direct_actor = root / "AV" / "japan" / "Direct Actor"
            nested_actor = root / "AV" / "japan" / "Nested Actor"
            (direct_actor / ".actors").mkdir(parents=True)
            (nested_actor / "TITLE-001" / ".actors").mkdir(parents=True)
            direct_image = direct_actor / ".actors" / "Direct Actor.jpg"
            nested_image = (
                nested_actor
                / "TITLE-001"
                / ".actors"
                / "Nested Actor.png"
            )
            direct_image.write_bytes(b"direct")
            nested_image.write_bytes(b"nested")

            library = MediaLibrary(root)

            self.assertEqual(
                library.actor_profile_for_directory(
                    "AV/japan/Direct Actor"
                ),
                "AV/japan/Direct Actor/.actors/Direct Actor.jpg",
            )
            self.assertEqual(
                library.actor_profile_for_directory(
                    "AV/japan/Nested Actor"
                ),
                "AV/japan/Nested Actor/TITLE-001/.actors/Nested Actor.png",
            )
            self.assertEqual(
                library.resolve_actor_image(
                    "AV/japan/Direct Actor/.actors/Direct Actor.jpg"
                ),
                direct_image.resolve(),
            )

    def test_actor_library_entries_report_media_and_subtitles(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            actor = root / "AV" / "japan" / "Actor"
            title = actor / "TITLE-001"
            (actor / ".actors").mkdir(parents=True)
            title.mkdir()
            (actor / ".actors" / "Actor.jpg").write_bytes(b"profile")
            (title / "done.mp4").write_bytes(b"media")
            (title / "done.ko.srt").write_text("subtitle", encoding="utf-8")
            (title / "pending.mkv").write_bytes(b"media")
            (actor / ".actors" / "ignored.mp4").write_bytes(b"metadata")

            entries = MediaLibrary(root).actor_library_entries()

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["name"], "Actor")
            self.assertEqual(entries[0]["path"], "AV/japan/Actor")
            self.assertEqual(
                entries[0]["image_path"],
                "AV/japan/Actor/.actors/Actor.jpg",
            )
            self.assertEqual(
                entries[0]["media"],
                [
                    {
                        "path": "AV/japan/Actor/TITLE-001/done.mp4",
                        "has_subtitle": True,
                        "has_external_subtitle": False,
                        "external_subtitle_formats": [],
                    },
                    {
                        "path": "AV/japan/Actor/TITLE-001/pending.mkv",
                        "has_subtitle": False,
                        "has_external_subtitle": False,
                        "external_subtitle_formats": [],
                    },
                ],
            )

    def test_collection_library_entry_reports_non_actor_media(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            show = root / "Variety" / "Show"
            show.mkdir(parents=True)
            (show / "done.mp4").write_bytes(b"media")
            (show / "done.ko.ass").write_text("subtitle", encoding="utf-8")
            (show / "pending.mkv").write_bytes(b"media")

            entry = MediaLibrary(root).collection_library_entry(
                "variety",
                name="버라이어티",
            )

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["name"], "버라이어티")
            self.assertEqual(entry["path"], "Variety")
            self.assertEqual(
                entry["media"],
                [
                    {
                        "path": "Variety/Show/done.mp4",
                        "has_subtitle": True,
                        "has_external_subtitle": False,
                        "external_subtitle_formats": [],
                    },
                    {
                        "path": "Variety/Show/pending.mkv",
                        "has_subtitle": False,
                        "has_external_subtitle": False,
                        "external_subtitle_formats": [],
                    },
                ],
            )

    def test_malformed_nfo_does_not_hide_media(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "movie.mkv").write_bytes(b"media")
            (root / "movie.nfo").write_text("<movie>", encoding="utf-8")

            files = MediaLibrary(root).browse()["files"]

            self.assertEqual(len(files), 1)
            self.assertTrue(files[0]["has_nfo"])
            self.assertEqual(files[0]["title"], "movie")
            self.assertIsNone(files[0]["poster_path"])

    def test_ignores_nfo_poster_outside_media_root(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "media"
            root.mkdir()
            (root / "movie.mkv").write_bytes(b"media")
            (root / "movie.nfo").write_text(
                "<movie><poster>../outside.jpg</poster></movie>",
                encoding="utf-8",
            )
            (base / "outside.jpg").write_bytes(b"poster")

            files = MediaLibrary(root).browse()["files"]

            self.assertIsNone(files[0]["poster_path"])

    def test_rejects_parent_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"media")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_file("../outside.mkv")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).browse("../")

    def test_rejects_poster_parent_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.jpg"
            outside.write_bytes(b"poster")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_poster("../outside.jpg")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_actor_image("../outside.jpg")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_rejects_symlink_to_file_outside_root(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"media")
            try:
                (root / "linked.mkv").symlink_to(outside)
            except OSError as error:
                self.skipTest(f"symlink creation is unavailable: {error}")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_file("linked.mkv")


class BackendSettingsTests(unittest.TestCase):
    def test_reads_backend_owned_translation_group_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TRANSLATION_BUILTIN_NAME": "로컬 LLM",
                "TRANSLATION_BUILTIN_BASE_URL": "http://translation.test/v1",
                "TRANSLATION_BUILTIN_TOKEN": "token",
                "TRANSLATION_BUILTIN_DRAFT_MODEL": "draft-model",
                "TRANSLATION_BUILTIN_REVIEW_MODEL": "review-model",
            },
            clear=True,
        ):
            settings = BackendSettings.from_env()

        self.assertEqual(settings.translation_builtin_name, "로컬 LLM")
        self.assertEqual(
            settings.translation_builtin_base_url,
            "http://translation.test/v1",
        )
        self.assertEqual(settings.translation_builtin_token, "token")
        self.assertEqual(settings.translation_builtin_draft_model, "draft-model")
        self.assertEqual(settings.translation_builtin_review_model, "review-model")
        self.assertEqual(
            settings.state_dir,
            Path("/var/lib/stt"),
        )
        self.assertEqual(settings.jobs_dir, Path("/var/lib/stt/jobs"))

    def test_ignores_legacy_combined_translation_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPATIBLE_BASE_URL": "http://legacy.test/v1",
                "OPENAI_COMPATIBLE_TOKEN": "legacy-token",
                "OPENAI_COMPATIBLE_MODEL": "legacy-model",
                "TRANSLATION_SERVICE_BASE_URL": "http://router.test/v1",
                "TRANSLATION_SERVICE_TOKEN": "router-token",
                "TRANSLATION_SERVICE_MODEL": "translation-router",
            },
            clear=True,
        ):
            settings = BackendSettings.from_env()

        self.assertEqual(settings.translation_builtin_base_url, "")
        self.assertEqual(settings.translation_builtin_token, "")

    def test_reads_backend_storage_directories(self) -> None:
        with patch.dict(
            os.environ,
            {"BACKEND_STATE_DIR": "/state", "BACKEND_WORK_DIR": "/work"},
            clear=True,
        ):
            settings = BackendSettings.from_env()

        self.assertEqual(settings.state_dir, Path("/state"))
        self.assertEqual(settings.jobs_dir, Path("/work"))

    def test_reads_and_validates_gpu_prometheus_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GPU_PROMETHEUS_URL": "https://prometheus.test/",
                "GPU_PROMETHEUS_TOKEN": "metric-token",
                "GPU_METRICS_REFRESH_SECONDS": "15",
                "GPU_METRICS_TIMEOUT_SECONDS": "2.5",
            },
            clear=True,
        ):
            settings = BackendSettings.from_env()

        settings.validate()
        self.assertEqual(
            settings.gpu_prometheus_url,
            "https://prometheus.test",
        )
        self.assertEqual(settings.gpu_prometheus_token, "metric-token")
        self.assertEqual(settings.gpu_metrics_refresh_seconds, 15.0)
        self.assertEqual(settings.gpu_metrics_timeout_seconds, 2.5)

    def test_rejects_non_http_gpu_prometheus_url(self) -> None:
        settings = BackendSettings(
            state_dir=Path("/state"),
            media_root=Path("/media"),
            stt_base_url="",
            stt_token="",
            gpu_prometheus_url="javascript:alert(1)",
        )

        with self.assertRaisesRegex(ValueError, "GPU_PROMETHEUS_URL"):
            settings.validate()

    def test_allows_server_configuration_after_startup(self) -> None:
        settings = BackendSettings(
            state_dir=Path("/state"),
            media_root=Path("/media"),
            stt_base_url="",
            stt_token="",
        )

        settings.validate()

    def test_allows_blank_credentials_on_trusted_network(self) -> None:
        settings = BackendSettings(
            state_dir=Path("/state"),
            media_root=Path("/media"),
            stt_base_url="http://stt.test",
            stt_token="",
        )

        settings.validate()

    def test_normalizes_remote_server_urls(self) -> None:
        settings = RemoteServerSettings(
            stt_base_url=" http://stt.test/ ",
            stt_token="stt-token",
        )

        normalized = settings.normalized()

        self.assertEqual(normalized.stt_base_url, "http://stt.test")

    def test_rejects_invalid_remote_server_url(self) -> None:
        settings = RemoteServerSettings(
            stt_base_url="file:///tmp/stt",
            stt_token="",
        )

        with self.assertRaisesRegex(ValueError, "STT_BASE_URL"):
            settings.normalized()

    def test_remote_server_settings_only_describe_transcription(self) -> None:
        settings = RemoteServerSettings(
            stt_base_url="http://runtime:8100",
            stt_token="",
        )

        normalized = settings.normalized()

        self.assertTrue(normalized.stt_is_complete)
        self.assertTrue(normalized.is_complete)
        self.assertEqual(set(vars(normalized)), {"stt_base_url", "stt_token"})

    def test_translation_builtin_capacity_must_be_between_one_and_eight(self) -> None:
        for capacity in (0, 9):
            settings = BackendSettings(
                state_dir=Path("/state"),
                media_root=Path("/media"),
                stt_base_url="",
                stt_token="",
                translation_builtin_capacity=capacity,
            )
            with self.assertRaisesRegex(ValueError, "1..8"):
                settings.validate()

    def test_normalizes_subtitle_validator_settings(self) -> None:
        settings = SubtitleValidatorSettings(
            base_url=" https://validator.test/v1/ ",
            token="secret",
            model=" paid-model ",
        )

        normalized = settings.normalized()

        self.assertEqual(normalized.base_url, "https://validator.test/v1")
        self.assertEqual(normalized.model, "paid-model")

    def test_normalizes_openrouter_subtitle_validator_settings(self) -> None:
        normalized = SubtitleValidatorSettings(
            provider="openrouter",
            base_url="https://ignored.test/v1",
            token="openrouter-key",
            model="anthropic/claude-sonnet",
        ).normalized()

        self.assertEqual(normalized.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(normalized.region, "")

    def test_normalizes_bedrock_subtitle_validator_settings(self) -> None:
        normalized = SubtitleValidatorSettings(
            provider="bedrock",
            token="bedrock-key",
            model="us.anthropic.claude-sonnet-4-6",
            region=" AP-NORTHEAST-2 ",
        ).normalized()

        self.assertEqual(normalized.base_url, "")
        self.assertEqual(normalized.region, "ap-northeast-2")
