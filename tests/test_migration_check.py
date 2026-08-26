from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.job_store import JobStore
from stt_to_subtitle.migration_check import dry_run_job_store_migrations


class MigrationDryRunTests(unittest.TestCase):
    def test_checks_an_isolated_backup_without_mutating_the_source(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            JobStore(database_path)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "DELETE FROM schema_migrations "
                    "WHERE name = 'correct_default_path_display_rule_v2'"
                )
                connection.execute(
                    """
                    UPDATE path_display_rules
                    SET source_pattern = ?, display_pattern = ?
                    WHERE id = 'default-actress-content'
                    """,
                    (
                        "{root}/{collection}/{actress}/{content_id}/{filename}",
                        "{actress}/{filename}",
                    ),
                )
            original = database_path.read_bytes()

            report = dry_run_job_store_migrations(database_path)

            self.assertEqual(database_path.read_bytes(), original)
            self.assertEqual(report["source"]["quick_check"], "ok")
            self.assertEqual(
                report["dry_run"]["applied"],
                [
                    {
                        "name": "correct_default_path_display_rule_v2",
                        "sequence": 40,
                    }
                ],
            )
            self.assertTrue(report["dry_run"]["integrity"]["valid"])

    def test_rejects_a_missing_source_database(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite3"

            with self.assertRaisesRegex(ValueError, "does not exist"):
                dry_run_job_store_migrations(missing)

            self.assertFalse(missing.exists())
