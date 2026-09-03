from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.storage_migration import migrate


class StorageMigrationTests(unittest.TestCase):
    def test_separates_audio_and_preserves_sqlite_and_artifacts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "legacy-state"
            work = root / "legacy-work"
            translation = root / "legacy-translation"
            target = root / "target"
            audio = root / "audio"
            for path in (state, work, translation, audio):
                path.mkdir()
            with sqlite3.connect(state / "jobs.sqlite3") as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('kept')")
            (work / "job-1").mkdir()
            (work / "job-1" / "audio.16k.wav").write_bytes(b"wav")
            (work / "job-1" / "transcript.json").write_text(
                '{"kept": true}',
                encoding="utf-8",
            )
            (translation / "routing.json").write_text(
                "{}",
                encoding="utf-8",
            )

            first = migrate(
                source_state=state,
                source_work=work,
                source_translation=translation,
                target_root=target,
                audio_root=audio,
            )
            second = migrate(
                source_state=state,
                source_work=work,
                source_translation=translation,
                target_root=target,
                audio_root=audio,
            )

            self.assertTrue((audio / "job-1" / "audio.16k.wav").is_file())
            self.assertFalse((target / "work" / "job-1" / "audio.16k.wav").exists())
            self.assertTrue((target / "work" / "job-1" / "transcript.json").is_file())
            with sqlite3.connect(target / "state" / "jobs.sqlite3") as connection:
                value = connection.execute("SELECT value FROM sample").fetchone()
            self.assertEqual(value, ("kept",))
            self.assertEqual(first["sqlite_integrity"]["status"], "ok")
            self.assertGreater(second["work"]["existing"], 0)

    def test_refuses_to_overwrite_a_different_existing_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            work = root / "work"
            translation = root / "translation"
            target = root / "target"
            audio = root / "audio"
            for path in (state, work, translation, audio):
                path.mkdir()
            (work / "result.json").write_text("source", encoding="utf-8")
            (target / "work").mkdir(parents=True)
            (target / "work" / "result.json").write_text(
                "different",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "already differs"):
                migrate(
                    source_state=state,
                    source_work=work,
                    source_translation=translation,
                    target_root=target,
                    audio_root=audio,
                )


if __name__ == "__main__":
    unittest.main()
