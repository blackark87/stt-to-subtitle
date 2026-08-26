from pathlib import Path
import unittest

from stt_to_subtitle.artifacts import artifact_filename, artifact_path


class ArtifactNamingTests(unittest.TestCase):
    def test_uses_source_based_json_names(self) -> None:
        self.assertEqual(
            artifact_filename("shows/episode.01.mkv", "transcript"),
            "episode.01_translate.json",
        )
        self.assertEqual(
            artifact_filename("shows/episode.01.mkv", "translation"),
            "episode.01_result_ko.json",
        )

    def test_isolates_equal_basenames_by_job_directory(self) -> None:
        jobs_dir = Path("/work/jobs")

        first = artifact_path(
            jobs_dir,
            "job-one",
            "show-a/episode.mkv",
            "translation",
        )
        second = artifact_path(
            jobs_dir,
            "job-two",
            "show-b/episode.mkv",
            "translation",
        )

        self.assertEqual(first.name, second.name)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(first.parent, jobs_dir / "job-one")
