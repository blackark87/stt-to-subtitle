from types import SimpleNamespace
import unittest

from stt_to_subtitle.library_progress import summarize_library_progress


class LibraryProgressTests(unittest.TestCase):
    def test_prioritizes_active_actors_and_builds_state_segments(self) -> None:
        entries = [
            {
                "name": "완료 배우",
                "path": "av/japan/done",
                "image_path": None,
                "media": [
                    {"path": "done.mp4", "has_subtitle": True},
                ],
            },
            {
                "name": "작업 배우",
                "path": "av/japan/active",
                "image_path": "av/japan/active/.actors/active.jpg",
                "media": [
                    {"path": "running.mp4", "has_subtitle": False},
                    {"path": "failed.mp4", "has_subtitle": False},
                    {"path": "new.mp4", "has_subtitle": False},
                ],
            },
        ]
        latest_jobs = {
            "running.mp4": SimpleNamespace(state="running"),
            "failed.mp4": SimpleNamespace(state="failed"),
        }

        summaries = summarize_library_progress(entries, latest_jobs)

        self.assertEqual(summaries[0]["name"], "작업 배우")
        self.assertEqual(summaries[0]["active"], 2)
        self.assertEqual(summaries[0]["attention"], 1)
        self.assertEqual(summaries[0]["remaining"], 3)
        self.assertEqual(
            [segment["state"] for segment in summaries[0]["segments"]],
            ["running", "attention", "unprocessed"],
        )

    def test_limits_the_number_of_actor_stacks(self) -> None:
        entries = [
            {
                "name": str(index),
                "path": str(index),
                "image_path": None,
                "media": [{"path": f"{index}.mp4", "has_subtitle": False}],
            }
            for index in range(8)
        ]

        self.assertEqual(len(summarize_library_progress(entries, {}, limit=5)), 5)

    def test_transcription_completion_is_not_subtitle_completion(self) -> None:
        entries = [
            {
                "name": "작업 배우",
                "path": "av/japan/actor",
                "image_path": None,
                "media": [
                    {"path": "transcribed.mp4", "has_subtitle": False},
                    {"path": "subtitled.mp4", "has_subtitle": False},
                ],
            }
        ]
        latest_jobs = {
            "transcribed.mp4": SimpleNamespace(
                state="done",
                status="transcription_completed",
            ),
            "subtitled.mp4": SimpleNamespace(
                state="done",
                status="completed",
            ),
        }

        summary = summarize_library_progress(entries, latest_jobs)[0]

        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["remaining"], 1)
        self.assertEqual(
            summary["segments"],
            [
                {"state": "done", "percent": 50.0},
                {"state": "unprocessed", "percent": 50.0},
            ],
        )


if __name__ == "__main__":
    unittest.main()
