import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import create_app
from database import get_db
from nomina.apply_vacation_requests import (
    VacationConnectorError,
    requested_days_and_rest_positions,
    require_test_database,
)


class VacationConnectorUnitTests(unittest.TestCase):
    def test_only_test_database_is_allowed(self):
        require_test_database("ctTecno_DEV")
        with self.assertRaises(VacationConnectorError):
            require_test_database("NomipaqTecno_All_")

    def test_encodes_sundays_as_one_based_positions(self):
        days, rest = requested_days_and_rest_positions(
            date(2026, 8, 13), date(2026, 8, 19)
        )
        self.assertEqual(days, 6)
        self.assertEqual(rest, "4")


class VacationConnectorApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "connector-test",
                "CONTPAQ_SYNC_TOKEN": "connector-token",
                "DATABASE_PATH": str(Path(self.tempdir.name) / "test.sqlite3"),
            }
        )
        self.client = self.app.test_client()
        with self.app.app_context():
            connection = get_db()
            connection.execute(
                """
                INSERT INTO users (
                    id, username, display_name, password_hash, access_role,
                    active, created_at, supervised_area
                ) VALUES (1, 'supervisor', 'Supervisor', 'hash', 'admin',
                          1, '2026-08-26T00:00:00Z', '')
                """
            )
            connection.execute(
                """
                INSERT INTO employees (
                    employee_name_key, employee_name, employee_code,
                    created_at, updated_at, last_seen_at
                ) VALUES ('ruben', 'Rubén', '017', '2026-08-26',
                          '2026-08-26', '2026-08-26')
                """
            )
            connection.execute(
                """
                INSERT INTO vacation_requests (
                    id, employee_name, employee_name_key, supervisor_id,
                    start_date, end_date, requested_days, status,
                    requested_at, responded_at, decided_by, contpaqi_status
                ) VALUES (7, 'Rubén', 'ruben', 1, '2026-09-01',
                          '2026-09-03', 3, 'approved', '2026-08-26T10:00:00Z',
                          '2026-08-26T10:05:00Z', 1, 'pending')
                """
            )
            connection.commit()

    def tearDown(self):
        self.tempdir.cleanup()

    @property
    def headers(self):
        return {"Authorization": "Bearer connector-token"}

    def test_claim_and_complete_request_are_idempotent(self):
        denied = self.client.post(
            "/api/integraciones/contpaqi/solicitudes/tomar"
        )
        self.assertEqual(denied.status_code, 401)

        claimed = self.client.post(
            "/api/integraciones/contpaqi/solicitudes/tomar",
            headers=self.headers,
        )
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.get_json()["employeeCode"], "017")
        empty = self.client.post(
            "/api/integraciones/contpaqi/solicitudes/tomar",
            headers=self.headers,
        )
        self.assertEqual(empty.status_code, 204)

        completed = self.client.post(
            "/api/integraciones/contpaqi/solicitudes/7/resultado",
            headers=self.headers,
            json={"status": "applied", "recordId": 14219999},
        )
        self.assertEqual(completed.status_code, 200)
        repeated = self.client.post(
            "/api/integraciones/contpaqi/solicitudes/7/resultado",
            headers=self.headers,
            json={"status": "applied", "recordId": 14219999},
        )
        self.assertTrue(repeated.get_json()["idempotent"])
        with self.app.app_context():
            row = get_db().execute(
                """
                SELECT contpaqi_status, contpaqi_record_id, contpaqi_attempts
                FROM vacation_requests WHERE id = 7
                """
            ).fetchone()
            self.assertEqual(row["contpaqi_status"], "applied")
            self.assertEqual(row["contpaqi_record_id"], 14219999)
            self.assertEqual(row["contpaqi_attempts"], 1)

    def test_failed_result_stays_available_for_manual_retry(self):
        self.client.post(
            "/api/integraciones/contpaqi/solicitudes/tomar",
            headers=self.headers,
        )
        response = self.client.post(
            "/api/integraciones/contpaqi/solicitudes/7/resultado",
            headers=self.headers,
            json={"status": "failed", "error": "Periodo traslapado"},
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            row = get_db().execute(
                "SELECT contpaqi_status, contpaqi_error FROM vacation_requests WHERE id = 7"
            ).fetchone()
            self.assertEqual(row["contpaqi_status"], "failed")
            self.assertEqual(row["contpaqi_error"], "Periodo traslapado")


if __name__ == "__main__":
    unittest.main()
