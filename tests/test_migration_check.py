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
                connection.commit()
                self.assertEqual(
                    connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()[0],
                    0,
                )
            database_path.with_name("jobs.sqlite3-wal").unlink(
                missing_ok=True
            )
            database_path.with_name("jobs.sqlite3-shm").unlink(
                missing_ok=True
            )
            original = database_path.read_bytes()
            original_files = {
                path.name: path.read_bytes()
                for path in database_path.parent.iterdir()
            }

            report = dry_run_job_store_migrations(database_path)

            self.assertEqual(database_path.read_bytes(), original)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in database_path.parent.iterdir()
                },
                original_files,
            )
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

    def test_reads_an_existing_wal_without_changing_source_sidecars(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            JobStore(database_path)
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("PRAGMA wal_autocheckpoint = 0")
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
                connection.commit()
                wal_path = database_path.with_name("jobs.sqlite3-wal")
                shm_path = database_path.with_name("jobs.sqlite3-shm")
                self.assertGreater(wal_path.stat().st_size, 0)
                self.assertTrue(shm_path.is_file())
                original_files = {
                    path.name: (
                        path.stat().st_uid,
                        path.stat().st_gid,
                        path.stat().st_mode & 0o777,
                    )
                    for path in database_path.parent.iterdir()
                }
                original_database = database_path.read_bytes()
                original_wal = wal_path.read_bytes()

                report = dry_run_job_store_migrations(database_path)

                self.assertEqual(
                    {
                        path.name: (
                            path.stat().st_uid,
                            path.stat().st_gid,
                            path.stat().st_mode & 0o777,
                        )
                        for path in database_path.parent.iterdir()
                    },
                    original_files,
                )
                self.assertEqual(database_path.read_bytes(), original_database)
                self.assertEqual(wal_path.read_bytes(), original_wal)
                self.assertEqual(
                    report["dry_run"]["applied"],
                    [
                        {
                            "name": "correct_default_path_display_rule_v2",
                            "sequence": 40,
                        }
                    ],
                )
            finally:
                connection.close()

    def test_rejects_a_nonempty_wal_without_shared_memory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "jobs.sqlite3"
            JobStore(database_path)
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("PRAGMA wal_autocheckpoint = 0")
                connection.execute(
                    "DELETE FROM schema_migrations "
                    "WHERE name = 'correct_default_path_display_rule_v2'"
                )
                connection.commit()
                wal_path = database_path.with_name("jobs.sqlite3-wal")
                self.assertGreater(wal_path.stat().st_size, 0)

                incomplete_root = root / "incomplete"
                incomplete_root.mkdir()
                incomplete_database = incomplete_root / "jobs.sqlite3"
                incomplete_wal = incomplete_root / "jobs.sqlite3-wal"
                incomplete_database.write_bytes(database_path.read_bytes())
                incomplete_wal.write_bytes(wal_path.read_bytes())
                original_files = {
                    path.name: path.read_bytes()
                    for path in incomplete_root.iterdir()
                }

                with self.assertRaisesRegex(
                    RuntimeError,
                    "active WAL without its shared-memory file",
                ):
                    dry_run_job_store_migrations(incomplete_database)

                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in incomplete_root.iterdir()
                    },
                    original_files,
                )
            finally:
                connection.close()

    def test_rejects_a_missing_source_database(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite3"

            with self.assertRaisesRegex(ValueError, "does not exist"):
                dry_run_job_store_migrations(missing)

            self.assertFalse(missing.exists())
