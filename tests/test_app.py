import tempfile
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

from app import (
    create_app,
    employee_photo_filename,
    save_employees,
    week_start_for,
    weekly_report_calendar,
)
from database import get_db


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

    def test_weekly_overtime_is_split_into_double_and_triple_hours(self):
        rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 23),
                "overtime_minutes": 480,
                "authorized_minutes": 480,
            },
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 24),
                "overtime_minutes": 120,
                "authorized_minutes": 120,
            },
        ]

        with self.app.app_context():
            workers, _ = weekly_report_calendar(rows, date(2026, 7, 23))

        self.assertEqual(workers[0]["cells"][0]["report"]["double_minutes"], 480)
        self.assertEqual(workers[0]["cells"][0]["report"]["triple_minutes"], 0)
        self.assertEqual(workers[0]["cells"][1]["report"]["double_minutes"], 60)
        self.assertEqual(workers[0]["cells"][1]["report"]["triple_minutes"], 60)

    def test_unapproved_overtime_is_not_counted_on_home(self):
        rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 23),
                "overtime_minutes": 90,
                "authorized_minutes": 0,
            }
        ]

        with self.app.app_context():
            workers, _ = weekly_report_calendar(rows, date(2026, 7, 23))

        report = workers[0]["cells"][0]["report"]
        self.assertEqual(report["counted_overtime_minutes"], 0)
        self.assertEqual(report["double_minutes"], 0)
        self.assertEqual(report["triple_minutes"], 0)

    def test_current_day_is_identified_separately_from_incomplete_punch(self):
        with self.app.app_context(), patch(
            "app.local_today", return_value=date(2026, 8, 4)
        ):
            workers, _ = weekly_report_calendar([], date(2026, 7, 30))

        current_cell = workers[0]["cells"][5]
        self.assertTrue(current_cell["is_today"])
        self.assertTrue(current_cell["is_incomplete"])

    def test_employee_photo_is_matched_by_filename_name(self):
        self.assertEqual(
            employee_photo_filename("Jorge Rangel Pulido"),
            "063 RANGEL PULIDO JORGE.jpg",
        )
        self.assertEqual(
            employee_photo_filename("Heidi Johana Reyez Diaz"),
            "095 REYES DIAZ HEIDI JOHANA.jpeg",
        )
        self.assertIsNone(
            employee_photo_filename("Trabajador Sin Fotografia"),
        )

    def test_home_searches_workers_and_shows_week_profile(self):
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
                    "clock_in": time(6, 58),
                    "clock_out": time(17, 2),
                    "overtime_minutes": 62,
                    "area": "Tijuana",
                }
            ]

        with patch("app.cached_attendance", side_effect=attendance_for):
            response = self.client.get("/?semana=2026-07-23")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Buscar trabajador", response.data)
        self.assertIn(b'class="home-page"', response.data)
        self.assertIn(b"tecnoall-logo.svg", response.data)
        self.assertIn(b"TecnoAll", response.data)
        self.assertIn(b"Jorge Rangel Pulido", response.data)
        self.assertIn(b"Habilitar tiempo extra", response.data)
        self.assertIn(b'id="home-enable-overtime-dialog"', response.data)
        self.assertIn(b"Informaci\xc3\xb3n adicional del trabajador", response.data)
        self.assertIn(b"data-additional-info", response.data)
        self.assertIn(b"06:58", response.data)
        self.assertIn(b"17:02", response.data)
        self.assertNotIn(b"1 h 02 min", response.data)
        self.assertIn(b"Horas dobles", response.data)
        self.assertIn(b"Horas triples", response.data)
        self.assertIn(b"0 h 00 min", response.data)
        self.assertNotIn(b"data-range=", response.data)
        self.assertIn(b"063%20RANGEL%20PULIDO%20JORGE.jpg", response.data)
        self.assertIn(b"home-worker-photo", response.data)
        self.assertNotIn(b"Espacios reservados", response.data)

    def test_home_can_enable_overtime_with_an_approved_hours_limit(self):
        self.initialize_admin()
        self.login()

        response = self.client.post(
            "/autorizaciones/desde-inicio",
            data={
                "csrf_token": self.csrf_token(),
                "employee_name_key": "ruben humberto lizarraga reyes",
                "work_date": "2026-08-06",
                "allowed_start": "17:00",
                "allowed_end": "20:00",
                "approved_hours": "1.5",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            b"trabajador=ruben+humberto+lizarraga+reyes",
            response.headers["Location"].encode(),
        )
        with self.app.app_context():
            authorization = get_db().execute(
                """
                SELECT allowed_start, allowed_end, approved_minutes
                FROM overtime_authorizations
                WHERE employee_name_key = ? AND work_date = ?
                """,
                (
                    "ruben humberto lizarraga reyes",
                    "2026-08-06",
                ),
            ).fetchone()
        self.assertEqual(authorization["allowed_start"], "17:00")
        self.assertEqual(authorization["allowed_end"], "20:00")
        self.assertEqual(authorization["approved_minutes"], 90)

        response = self.client.post(
            "/autorizaciones/desde-inicio/eliminar",
            data={
                "csrf_token": self.csrf_token(),
                "employee_name_key": "ruben humberto lizarraga reyes",
                "work_date": "2026-08-06",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute(
                "SELECT COUNT(*) AS total FROM overtime_authorizations"
            ).fetchone()["total"]
        self.assertEqual(total, 0)

    def test_home_shows_authorized_overtime_button_and_detail(self):
        self.initialize_admin()
        self.login()
        report_rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 23),
                "clock_in": time(7, 0),
                "clock_out": time(18, 30),
                "overtime_minutes": 90,
                "authorized_minutes": 60,
                "unauthorized_minutes": 30,
                "allowed_range": "17:00–18:00",
                "area": "Tijuana",
            }
        ]

        with patch(
            "app.report_for_week",
            return_value=(report_rows, None, {date(2026, 7, 23)}),
        ):
            response = self.client.get("/?semana=2026-07-23")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"1 h 00 min", response.data)
        self.assertNotIn(b"1 h 30 min", response.data)
        self.assertIn(b"data-home-authorization", response.data)
        self.assertIn(b"Horas extra aprobadas", response.data)
        self.assertIn(b"Eliminar tiempo extra autorizado", response.data)
        self.assertIn(b"Horas extras autorizadas", response.data)
        self.assertIn("17:00–18:00".encode(), response.data)
        self.assertIn(b'id="home-authorization-dialog"', response.data)

    def test_home_keeps_directory_worker_without_attendance_visible(self):
        self.initialize_admin()
        self.login()

        with patch("app.cached_attendance", return_value=[]):
            response = self.client.get("/?semana=2026-07-30")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ruben Humberto Lizarraga Reyes", response.data)
        self.assertIn(
            b"017%20LIZARRAGA%20REYES%20RUBEN%20HUMBERTO.jpg",
            response.data,
        )

    def test_legacy_authorization_pages_redirect_without_changes(self):
        self.initialize_admin()
        self.login()

        for method, path in (
            ("get", "/autorizaciones"),
            ("get", "/autorizaciones/nueva"),
            ("post", "/autorizaciones/nueva"),
            ("post", "/autorizaciones/123/eliminar"),
        ):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/"))

        with self.app.app_context():
            total = get_db().execute(
                "SELECT COUNT(*) AS total FROM overtime_authorizations"
            ).fetchone()["total"]
        self.assertEqual(total, 0)

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
        with self.app.app_context():
            save_employees(
                [{
                    "employee_name": "Mario Ángel Hernández",
                    "employee_name_key": "mario angel hernandez",
                }]
            )
        self.client.post(
            "/autorizaciones/desde-inicio",
            data={
                "csrf_token": self.csrf_token(),
                "employee_name_key": "mario angel hernandez",
                "work_date": "2026-07-27",
                "allowed_start": "17:00",
                "allowed_end": "19:00",
                "approved_hours": "2",
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
        self.assertIn(b'class="report-calendar"', response.data)
        self.assertIn(b"data-report-worker", response.data)
        self.assertIn("✓ Horas extra permitidas".encode(), response.data)
        self.assertIn("17:00–19:00".encode(), response.data)
        self.assertIn(b"Horas autorizadas utilizadas", response.data)
        self.assertNotIn(b"Horas extra detectadas", response.data)
        self.assertNotIn(b"<th>Estado</th>", response.data)

    def test_loaded_workers_are_saved_and_can_be_assigned_to_an_area(self):
        self.initialize_admin()
        self.login()

        with self.app.app_context():
            save_employees(
                [{
                    "employee_name": "Ana López",
                    "employee_name_key": "ana lopez",
                }]
            )

        response = self.client.get("/trabajadores")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ana López".encode(), response.data)
        self.assertIn(b"Compras/Ventas", response.data)
        self.assertIn(b"Compras/Ventas Mxli", response.data)
        self.assertIn(b"Contabilidad", response.data)
        self.assertIn("Almacén".encode(), response.data)
        self.assertEqual(response.data.count(b"Guardar todas las \xc3\xa1reas"), 1)
        self.assertIn(b"data-employees-area-filter", response.data)
        self.assertIn(b'data-employee-area="__none__"', response.data)

        response = self.client.post(
            "/trabajadores/areas",
            data={
                "csrf_token": self.csrf_token(),
                "employee_name_key": [
                    "ana lopez",
                    "ruben humberto lizarraga reyes",
                ],
                "area": ["Ingeniería", "Abquim"],
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"fueron actualizadas", response.data)

        with self.app.app_context():
            save_employees(
                [{
                    "employee_name": "ANA LÓPEZ",
                    "employee_name_key": "ana lopez",
                }]
            )
            employee = get_db().execute(
                """
                SELECT employee_name, area FROM employees
                WHERE employee_name_key = 'ana lopez'
                """
            ).fetchone()
            self.assertEqual(employee["employee_name"], "ANA LÓPEZ")
            self.assertEqual(employee["area"], "Ingeniería")
            ruben = get_db().execute(
                """
                SELECT area FROM employees
                WHERE employee_name_key = 'ruben humberto lizarraga reyes'
                """
            ).fetchone()
            self.assertEqual(ruben["area"], "Abquim")

        with patch("app.cached_attendance", return_value=[]):
            response = self.client.get("/?semana=2026-07-23")
        self.assertIn(b'data-worker-area="ingenier\xc3\xada"', response.data)
        self.assertIn(b"data-home-area-filter", response.data)

        stylesheet = self.client.get("/static/app.css")
        self.assertIn(b".is-filtered-out", stylesheet.data)
        stylesheet.close()

    def test_report_calendar_marks_incomplete_punches(self):
        self.initialize_admin()
        self.login()

        def attendance_for(day, force=False):
            if day != date(2026, 7, 23):
                return []
            return [
                {
                    "employee_name": "Eduardo Sanchez Reyna",
                    "employee_name_key": "eduardo sanchez reyna",
                    "work_date": day,
                    "clock_in": time(7, 43),
                    "clock_out": None,
                    "overtime_minutes": 0,
                    "area": "Tijuana",
                }
            ]

        with patch("app.cached_attendance", side_effect=attendance_for):
            response = self.client.get(
                "/reporte?semana=2026-07-23"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Eduardo Sanchez Reyna", response.data)
        self.assertIn("⚠ Checada incompleta".encode(), response.data)
        self.assertIn(b"07:43", response.data)
        self.assertIn(b'id="report-detail-dialog"', response.data)


if __name__ == "__main__":
    unittest.main()
