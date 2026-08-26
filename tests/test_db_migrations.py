import sqlite3
import unittest

from stt_to_subtitle.db_migrations import (
    Migration,
    execute_sql_statements,
    ordered_migrations,
    run_migrations,
)


class MigrationRunnerTests(unittest.TestCase):
    def test_runs_missing_migrations_in_sequence_order_once(self) -> None:
        connection = sqlite3.connect(":memory:")
        applied: list[str] = []
        migrations = (
            Migration(20, "second_v1", lambda _connection: applied.append("2")),
            Migration(10, "first_v1", lambda _connection: applied.append("1")),
        )

        first = run_migrations(connection, migrations)
        second = run_migrations(connection, migrations)

        self.assertEqual(first, ("first_v1", "second_v1"))
        self.assertEqual(second, ())
        self.assertEqual(applied, ["1", "2"])
        self.assertEqual(
            [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM schema_migrations ORDER BY sequence"
                )
            ],
            ["first_v1", "second_v1"],
        )
        connection.close()

    def test_rejects_ambiguous_or_invalid_migration_definitions(self) -> None:
        callback = lambda _connection: None

        with self.assertRaisesRegex(ValueError, "sequences must be unique"):
            ordered_migrations(
                (
                    Migration(1, "first_v1", callback),
                    Migration(1, "second_v1", callback),
                )
            )
        with self.assertRaisesRegex(ValueError, "names must be unique"):
            ordered_migrations(
                (
                    Migration(1, "same_v1", callback),
                    Migration(2, "same_v1", callback),
                )
            )
        with self.assertRaisesRegex(ValueError, "invalid migration name"):
            ordered_migrations((Migration(1, "Invalid name", callback),))

    def test_backfills_legacy_ledger_and_rejects_sequence_changes(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES ('first_v1', 1)"
        )
        migrations = (Migration(10, "first_v1", lambda _connection: None),)

        self.assertEqual(run_migrations(connection, migrations), ())
        sequence = connection.execute(
            "SELECT sequence FROM schema_migrations WHERE name = 'first_v1'"
        ).fetchone()[0]
        self.assertEqual(sequence, 10)

        with self.assertRaisesRegex(RuntimeError, "sequence changed"):
            run_migrations(
                connection,
                (Migration(20, "first_v1", lambda _connection: None),),
            )
        connection.close()

    def test_rolls_back_a_failed_migration_without_recording_it(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE values_table (value TEXT)")

        def fail_after_write(database: sqlite3.Connection) -> None:
            database.execute("INSERT INTO values_table VALUES ('partial')")
            raise RuntimeError("migration failed")

        with self.assertRaisesRegex(RuntimeError, "migration failed"):
            run_migrations(
                connection,
                (Migration(10, "failing_v1", fail_after_write),),
            )

        self.assertEqual(
            connection.execute("SELECT * FROM values_table").fetchall(),
            [],
        )
        self.assertEqual(
            connection.execute("SELECT * FROM schema_migrations").fetchall(),
            [],
        )
        connection.close()

    def test_executes_sql_statements_without_splitting_quoted_semicolons(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")

        execute_sql_statements(
            connection,
            """
            CREATE TABLE examples (value TEXT);
            INSERT INTO examples VALUES ('first;second');
            """,
        )

        self.assertEqual(
            connection.execute("SELECT value FROM examples").fetchone()[0],
            "first;second",
        )
        with self.assertRaisesRegex(ValueError, "incomplete SQL"):
            execute_sql_statements(connection, "SELECT '")
        connection.close()
