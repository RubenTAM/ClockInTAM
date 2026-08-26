import tempfile
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from database import get_db
from nomina.provision_worker_accounts import provision_worker_accounts


class WorkerProvisionTests(unittest.TestCase):
    def test_creates_only_group_workers_and_resets_existing_account(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "workers.sqlite3")
            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "provision-test",
                    "DATABASE_PATH": database_path,
                }
            )
            with app.app_context():
                connection = get_db()
                now = "2026-08-26T00:00:00Z"
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO employees (
                        employee_name_key, employee_name, employee_code, area,
                        created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("one", "Trabajador Uno", "013", "TecnoAll - Ingenieria", now, now, now),
                        ("two", "Trabajador Dos", "014", "TecnoAll - Ingenieria", now, now, now),
                        ("other", "Otra Persona", "099", "TecnoAll - Compras", now, now, now),
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO users (
                        username, display_name, password_hash, access_role,
                        employee_name_key, created_at, supervised_area
                    ) VALUES (?, ?, ?, 'worker', ?, ?, '')
                    """,
                    (
                        "existing@example.com", "Trabajador Uno",
                        generate_password_hash(
                            "ClaveAnterior123", method="pbkdf2:sha256"
                        ),
                        "one", now,
                    ),
                )
                connection.commit()

            result = provision_worker_accounts(
                database_path,
                group="TecnoAll - Ingenieria",
                temporary_password="123456789",
            )
            self.assertEqual(result["created"], ["014"])
            self.assertEqual(result["reset"], ["existing@example.com"])
            with app.app_context():
                rows = get_db().execute(
                    """
                    SELECT username, password_hash, must_change_password
                    FROM users WHERE access_role = 'worker'
                    ORDER BY employee_name_key
                    """
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row["must_change_password"] for row in rows))
                self.assertTrue(
                    all(
                        check_password_hash(row["password_hash"], "123456789")
                        for row in rows
                    )
                )


if __name__ == "__main__":
    unittest.main()
