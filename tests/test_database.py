import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app
from database import get_db


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_authorizations_survive_multiple_interval_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE overtime_authorizations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_name TEXT NOT NULL,
                    employee_name_key TEXT NOT NULL,
                    work_date TEXT NOT NULL,
                    allowed_start TEXT NOT NULL,
                    allowed_end TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (created_by) REFERENCES users(id),
                    UNIQUE(employee_name_key, work_date)
                );

                INSERT INTO users (
                    id, username, display_name, password_hash, created_at
                ) VALUES (
                    1, 'admin', 'Administrador', 'hash', '2026-07-27T00:00:00Z'
                );

                INSERT INTO overtime_authorizations (
                    employee_name, employee_name_key, work_date,
                    allowed_start, allowed_end, note, created_by,
                    created_at, updated_at
                ) VALUES (
                    'Jorge Rangel Pulido', 'jorge rangel pulido',
                    '2026-07-27', '07:00', '08:00', '', 1,
                    '2026-07-27T00:00:00Z', '2026-07-27T00:00:00Z'
                );
                """
            )
            connection.commit()
            connection.close()

            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "migration-test",
                    "DATABASE_PATH": str(database_path),
                }
            )
            with app.app_context():
                migrated = get_db()
                migrated.execute(
                    """
                    INSERT INTO overtime_authorizations (
                        employee_name, employee_name_key, work_date,
                        allowed_start, allowed_end, note, created_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "Jorge Rangel Pulido",
                        "jorge rangel pulido",
                        "2026-07-27",
                        "17:00",
                        "19:00",
                        "",
                        1,
                        "2026-07-27T01:00:00Z",
                        "2026-07-27T01:00:00Z",
                    ),
                )
                migrated.commit()
                total = migrated.execute(
                    "SELECT COUNT(*) AS total FROM overtime_authorizations"
                ).fetchone()["total"]
                columns = {
                    row["name"]
                    for row in migrated.execute(
                        "PRAGMA table_info('overtime_authorizations')"
                    ).fetchall()
                }

            self.assertEqual(total, 2)
            self.assertIn("approved_minutes", columns)


if __name__ == "__main__":
    unittest.main()
