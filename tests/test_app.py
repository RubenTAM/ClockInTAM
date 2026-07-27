import tempfile
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

from app import create_app, week_start_for


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE_PATH": str(
                    Path(self.tempdir.name) / "test.sqlite3"
                ),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def csrf_token(self):
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def initialize_admin(self):
        self.client.get("/setup")
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(),
                "display_name": "Administrador General",
                "username": "admin",
                "password": "UnaClaveSegura123",
                "password_confirmation": "UnaClaveSegura123",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Iniciar sesi", response.data)

    def login(self):
        self.client.get("/login")
        return self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "admin",
                "password": "UnaClaveSegura123",
            },
            follow_redirects=True,
        )

    def test_first_visit_requires_setup(self):
        response = self.client.get("/", follow_redirects=True)
        self.assertIn(b"Crea al administrador principal", response.data)

    def test_operational_week_runs_thursday_to_wednesday(self):
        self.assertEqual(
            week_start_for(date(2026, 7, 27)),
            date(2026, 7, 23),
        )
        self.assertEqual(
            week_start_for(date(2026, 7, 29)),
            date(2026, 7, 23),
        )
        self.assertEqual(
            week_start_for(date(2026, 7, 30)),
            date(2026, 7, 30),
        )

    def test_admin_can_create_batch_authorizations(self):
        self.initialize_admin()
        response = self.login()
        self.assertIn(b"Reporte semanal", response.data)

        self.client.get("/autorizaciones/nueva")
        response = self.client.post(
            "/autorizaciones/nueva",
            data={
                "csrf_token": self.csrf_token(),
                "employee_names": [
                    "Mario Ángel Hernández",
                    "Jorge Rangel",
                ],
                "work_dates": ["2026-07-27", "2026-07-28"],
                "allowed_start": "17:00",
                "allowed_end": "19:00",
                "note": "Cierre semanal",
                "reference_date": "2026-07-22",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Se guardaron 4 autorizaciones", response.data)
        self.assertIn("Mario Ángel Hernández".encode(), response.data)
        self.assertIn("17:00–19:00".encode(), response.data)

    def test_authorization_page_uses_weekly_calendar(self):
        self.initialize_admin()
        self.login()

        def attendance_for(day, force=False):
            if day != date(2026, 7, 23):
                return []
            return [
                {
                    "employee_name": "Jorge Rangel Pulido",
                    "employee_name_key": "jorge rangel pulido",
                    "work_date": day,
                    "clock_in": time(7, 0),
                    "clock_out": time(17, 0),
                    "overtime_minutes": 60,
                    "area": "Tijuana",
                }
            ]

        with patch("app.cached_attendance", side_effect=attendance_for):
            response = self.client.get(
                "/autorizaciones?semana=2026-07-23"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Jorge Rangel Pulido", response.data)
        self.assertEqual(
            response.data.count(b"Sin registro de autorizaci"),
            7,
        )
        self.assertIn(b'data-date="2026-07-23"', response.data)
        self.assertIn(b'data-date="2026-07-29"', response.data)
        self.assertIn(b"Guardar autorizaci", response.data)

    def test_login_only_shows_requested_fields(self):
        self.initialize_admin()
        response = self.client.get("/login")
        self.assertIn("¡Hola!".encode(), response.data)
        self.assertIn(b"Usuario", response.data)
        self.assertIn("Contraseña".encode(), response.data)
        self.assertIn(b"Iniciar sesi", response.data)
        self.assertNotIn(b"Tiempo", response.data)
        self.assertNotIn(b"Control de horas extra", response.data)
        self.assertNotIn(b"Consulta checadas", response.data)
        self.assertIn(b"/static/app.css?v=", response.data)
        self.assertIn(b"/static/app.js?v=", response.data)

    def test_csrf_is_required(self):
        self.initialize_admin()
        self.login()
        response = self.client.post("/logout", data={})
        self.assertEqual(response.status_code, 400)

    def test_weekly_report_compares_live_rows_with_authorization(self):
        self.initialize_admin()
        self.login()
        self.client.get("/autorizaciones/nueva")
        self.client.post(
            "/autorizaciones/nueva",
            data={
                "csrf_token": self.csrf_token(),
                "employee_names": ["Mario Ángel Hernández"],
                "work_dates": ["2026-07-27"],
                "allowed_start": "17:00",
                "allowed_end": "19:00",
                "note": "",
                "reference_date": "2026-07-22",
            },
        )

        def attendance_for(day, force=False):
            if day != date(2026, 7, 27):
                return []
            return [
                {
                    "employee_name": "MARIO ANGEL HERNANDEZ",
                    "employee_name_key": "mario angel hernandez",
                    "work_date": day,
                    "clock_in": time(7, 0),
                    "clock_out": time(20, 0),
                    "overtime_minutes": 180,
                    "area": "Tijuana",
                }
            ]

        with patch("app.cached_attendance", side_effect=attendance_for):
            response = self.client.get(
                "/reporte?semana=2026-07-27"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 h 00 min", response.data)
        self.assertIn(b"1 h 00 min", response.data)
        self.assertNotIn(b"Horas extra detectadas", response.data)
        self.assertNotIn(b"<th>Estado</th>", response.data)


if __name__ == "__main__":
    unittest.main()
