import tempfile
import unittest
import io
import zipfile
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import (
    attendance_incident_labels,
    create_app,
    employee_photo_filename,
    known_employee_code,
    requested_vacation_days,
    rounded_overtime_minutes,
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
                "CONTPAQ_SYNC_TOKEN": "sync-test-token",
                "PAYROLL_DOWNLOAD_ENABLED": True,
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

    def test_requested_vacation_days_excludes_sundays(self):
        self.assertEqual(
            requested_vacation_days(date(2026, 9, 1), date(2026, 9, 7)),
            6,
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

    def test_home_rounds_overtime_only_from_fifty_minutes(self):
        rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 23),
                "authorized_minutes": 49,
                "approved_minutes": 49,
            },
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 24),
                "authorized_minutes": 50,
                "approved_minutes": 50,
            },
        ]

        with self.app.app_context():
            workers, _ = weekly_report_calendar(rows, date(2026, 7, 23))

        first_report = workers[0]["cells"][0]["report"]
        second_report = workers[0]["cells"][1]["report"]
        self.assertEqual(rounded_overtime_minutes(49), 0)
        self.assertEqual(rounded_overtime_minutes(50), 60)
        self.assertEqual(first_report["rounded_counted_overtime_minutes"], 0)
        self.assertEqual(first_report["rounded_double_minutes"], 0)
        self.assertEqual(first_report["rounded_approved_minutes"], 0)
        self.assertEqual(second_report["rounded_counted_overtime_minutes"], 60)
        self.assertEqual(second_report["rounded_double_minutes"], 60)
        self.assertEqual(second_report["rounded_approved_minutes"], 60)

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

    def test_attendance_incidents_include_late_and_early_punches(self):
        monday = date(2026, 8, 17)

        self.assertEqual(
            attendance_incident_labels(
                monday,
                {"clock_in": time(8, 10), "clock_out": time(17, 0)},
            ),
            [],
        )
        self.assertEqual(
            attendance_incident_labels(
                monday,
                {"clock_in": time(8, 11), "clock_out": None},
            ),
            ["Llegada tarde", "No checó salida"],
        )
        self.assertEqual(
            attendance_incident_labels(
                monday,
                {"clock_in": time(8, 0), "clock_out": time(16, 59)},
            ),
            ["Salida temprana"],
        )

    def test_incident_permission_suppresses_attendance_but_not_overtime(self):
        monday = date(2026, 8, 17)
        rows = [{
            "employee_name": "Jorge Rangel Pulido",
            "employee_name_key": "jorge rangel pulido",
            "work_date": monday,
            "clock_in": time(8, 11),
            "clock_out": None,
            "authorized_minutes": 60,
            "approved_minutes": 60,
        }]
        permissions = [{
            "employee_name_key": "jorge rangel pulido",
            "work_date": monday.isoformat(),
        }]

        with self.app.app_context(), patch(
            "app.local_today", return_value=date(2026, 8, 18)
        ):
            workers, _ = weekly_report_calendar(
                rows,
                date(2026, 8, 13),
                permission_rows=permissions,
            )

        worker = next(
            item for item in workers
            if item["employee_name_key"] == "jorge rangel pulido"
        )
        cell = worker["cells"][4]
        self.assertTrue(cell["has_incident_permission"])
        self.assertEqual(cell["incident_labels"], [])
        self.assertFalse(cell["has_attendance_incident"])
        self.assertEqual(
            cell["report"]["rounded_counted_overtime_minutes"], 60
        )
        self.assertTrue(worker["has_preview_incidents"])

    def test_preview_incident_flag_ignores_normal_days(self):
        work_days = [
            date(2026, 8, 13),
            date(2026, 8, 14),
            date(2026, 8, 15),
            date(2026, 8, 17),
            date(2026, 8, 18),
            date(2026, 8, 19),
        ]
        rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": work_date,
                "clock_in": time(8, 0),
                "clock_out": time(17, 0),
                "authorized_minutes": 0,
                "approved_minutes": 0,
            }
            for work_date in work_days
        ]
        with self.app.app_context(), patch(
            "app.local_today", return_value=date(2026, 8, 20)
        ):
            workers, _ = weekly_report_calendar(
                rows, date(2026, 8, 13)
            )
            worker = next(
                item for item in workers
                if item["employee_name_key"] == "jorge rangel pulido"
            )
            self.assertFalse(worker["has_preview_incidents"])

            rows[0]["authorized_minutes"] = 60
            rows[0]["approved_minutes"] = 60
            workers, _ = weekly_report_calendar(
                rows, date(2026, 8, 13)
            )
            worker = next(
                item for item in workers
                if item["employee_name_key"] == "jorge rangel pulido"
            )
            self.assertTrue(worker["has_preview_incidents"])

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

    def test_employee_codes_match_name_order_and_known_variants(self):
        self.assertEqual(known_employee_code("Jorge Rangel Pulido"), "063")
        self.assertEqual(known_employee_code("Heidi Johana Reyez Diaz"), "095")
        self.assertEqual(known_employee_code("Luis Gustavo Lopez Feliz"), "024")
        self.assertEqual(known_employee_code("Trabajador Pendiente"), "")

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
        self.assertIn(b"Horario establecido", response.data)
        self.assertIn(b'id="home-established-schedule"', response.data)
        self.assertIn(b"Informaci\xc3\xb3n adicional del trabajador", response.data)
        self.assertIn(b"data-additional-info", response.data)
        self.assertIn(
            "Días de vacaciones disponibles".encode(), response.data
        )
        self.assertIn(b"Pendiente de sincronizar", response.data)
        self.assertNotIn(b"<dt>Correo</dt>", response.data)
        self.assertNotIn("Teléfono".encode(), response.data)
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

    def test_contpaqi_vacation_sync_updates_worker_profile(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            save_employees(
                [
                    {
                        "employee_name": "Jorge Rangel Pulido",
                        "employee_name_key": "jorge rangel pulido",
                        "group_name": "TecnoAll - Ingeniería",
                    }
                ]
            )

        response = self.client.post(
            "/api/integraciones/contpaqi/vacaciones",
            headers={"Authorization": "Bearer sync-test-token"},
            json={
                "source": "CONTPAQi Nóminas",
                "asOf": "2026-08-25",
                "balances": [
                    {
                        "employeeId": 63,
                        "employeeCode": "63",
                        "employeeName": "RANGEL PULIDO JORGE",
                        "employeeStatus": "A",
                        "availableDays": 8.5,
                        "movements": [
                            {
                                "sourceMovementKey": "vacation:77",
                                "concept": "Vacaciones tomadas",
                                "registeredDate": "2026-08-10",
                                "startDate": "2026-08-10",
                                "endDate": "2026-08-10",
                                "daysTaken": 1,
                                "daysEntitled": 0,
                                "balance": 8.5,
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["updated"], 1)
        self.assertEqual(response.get_json()["movementsUpdated"], 1)

        with patch("app.cached_attendance", return_value=[]):
            home = self.client.get("/?semana=2026-08-20")
        self.assertEqual(home.status_code, 200)
        self.assertIn("8.5 días".encode(), home.data)

        with self.app.app_context():
            employee = get_db().execute(
                """
                SELECT vacation_days_available, vacation_balance_as_of,
                       vacation_source, contpaqi_employee_id,
                       contpaqi_employee_name
                FROM employees WHERE employee_code = '063'
                """
            ).fetchone()
            movement = get_db().execute(
                """
                SELECT concept, start_date, days_taken, balance
                FROM employee_vacation_movements
                WHERE employee_name_key = 'jorge rangel pulido'
                """
            ).fetchone()
        self.assertEqual(employee["vacation_days_available"], 8.5)
        self.assertEqual(employee["vacation_balance_as_of"], "2026-08-25")
        self.assertEqual(employee["vacation_source"], "CONTPAQi Nóminas")
        self.assertEqual(employee["contpaqi_employee_id"], 63)
        self.assertEqual(
            employee["contpaqi_employee_name"], "RANGEL PULIDO JORGE"
        )
        self.assertEqual(movement["concept"], "Vacaciones tomadas")
        self.assertEqual(movement["start_date"], "2026-08-10")
        self.assertEqual(movement["days_taken"], 1)
        self.assertEqual(movement["balance"], 8.5)

    def test_contpaqi_vacation_sync_requires_token(self):
        response = self.client.post(
            "/api/integraciones/contpaqi/vacaciones",
            json={"asOf": "2026-08-25", "balances": []},
        )
        self.assertEqual(response.status_code, 401)

    def test_contpaqi_sync_can_tag_unique_worker_by_name(self):
        self.initialize_admin()
        with self.app.app_context():
            save_employees(
                [
                    {
                        "employee_name": "Trabajadora Ejemplo Única",
                        "employee_name_key": "trabajadora ejemplo unica",
                        "group_name": "TecnoAll - Pruebas",
                    }
                ]
            )

        response = self.client.post(
            "/api/integraciones/contpaqi/vacaciones",
            headers={"Authorization": "Bearer sync-test-token"},
            json={
                "source": "CONTPAQi Nóminas",
                "asOf": "2026-08-25",
                "balances": [
                    {
                        "employeeId": 901,
                        "employeeCode": "0901",
                        "employeeName": "EJEMPLO UNICA TRABAJADORA",
                        "employeeStatus": "A",
                        "availableDays": 12,
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["matchedByName"], 1)
        with self.app.app_context():
            employee = get_db().execute(
                """
                SELECT employee_code, contpaqi_employee_id,
                       vacation_days_available
                FROM employees
                WHERE employee_name_key = 'trabajadora ejemplo unica'
                """
            ).fetchone()
        self.assertEqual(employee["employee_code"], "0901")
        self.assertEqual(employee["contpaqi_employee_id"], 901)
        self.assertEqual(employee["vacation_days_available"], 12)

    def test_engineering_supervisor_sees_team_incident_summary(self):
        self.client.get("/setup")
        self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(),
                "display_name": "Rubén Lizarraga",
                "username": "ruben",
                "password": "UnaClaveSegura123",
                "password_confirmation": "UnaClaveSegura123",
            },
        )
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "ruben",
                "password": "UnaClaveSegura123",
            },
        )
        with self.app.app_context():
            save_employees(
                [
                    {
                        "employee_name": "Jorge Rangel Pulido",
                        "employee_name_key": "jorge rangel pulido",
                        "group_name": "TecnoAll - Ingenieria",
                    },
                    {
                        "employee_name": "Ana López",
                        "employee_name_key": "ana lopez",
                        "group_name": "TecnoAll - Compras",
                    },
                ]
            )

        report_rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 23),
                "clock_in": time(8, 5),
                "clock_out": time(18, 0),
                "overtime_minutes": 60,
                "authorized_minutes": 60,
                "approved_minutes": 120,
                "unauthorized_minutes": 45,
                "unused_minutes": 0,
            },
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 24),
                "clock_in": time(8, 0),
                "clock_out": time(17, 0),
                "overtime_minutes": 0,
                "authorized_minutes": 0,
                "approved_minutes": 0,
                "unauthorized_minutes": 0,
                "unused_minutes": 0,
            },
        ]
        with patch(
            "app.report_for_week",
            return_value=(report_rows, None, {date(2026, 7, 23)}),
        ), patch("app.local_today", return_value=date(2026, 7, 30)):
            response = self.client.get("/?semana=2026-07-23")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Vista de trabajadores", response.data)
        self.assertIn(b"data-home-browser", response.data)
        self.assertIn(b"data-home-preview", response.data)
        self.assertIn(b"Asistencia del equipo", response.data)
        self.assertIn(b'data-supervisor-preview-filter', response.data)
        self.assertIn(b'>Con incidencias</option>', response.data)
        self.assertIn(b'>Todos del grupo</option>', response.data)
        self.assertIn(b'data-preview-has-incidents="1"', response.data)
        self.assertNotIn(b"Checadas faltantes</span>", response.data)
        self.assertNotIn(b"Total trabajadores</span>", response.data)
        self.assertIn(b"Jorge Rangel Pulido", response.data)
        self.assertNotIn("Ana López".encode(), response.data)
        self.assertIn(b"+ 1 h extra utilizada", response.data)
        self.assertNotIn(b"+ 2 h extras utilizadas", response.data)
        overtime_cell = response.data.index(b"+ 1 h extra utilizada")
        previous_cell = response.data.rfind(b'<td class="supervisor-day-cell">', 0, overtime_cell)
        self.assertNotIn(
            b"Sin incidencias",
            response.data[previous_cell:overtime_cell],
        )
        self.assertIn("No checó entrada".encode(), response.data)
        self.assertIn("No checó salida".encode(), response.data)
        self.assertIn(b"Sin incidencias", response.data)
        self.assertNotIn(b"sin autorizar", response.data)
        self.assertIn(b"Informaci\xc3\xb3n del trabajador", response.data)
        self.assertLess(
            response.data.index(b"Vista previa"),
            response.data.index(b"Jorge Rangel Pulido"),
        )

        with self.app.app_context():
            user = get_db().execute(
                "SELECT supervised_area FROM users WHERE username = 'ruben'"
            ).fetchone()
            self.assertEqual(
                user["supervised_area"], "TecnoAll - Ingenieria"
            )

    def test_new_user_with_full_access_sees_all_worker_groups(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            save_employees(
                [
                    {
                        "employee_name": "Trabajador Ingeniería Prueba",
                        "employee_name_key": "trabajador ingenieria prueba",
                        "group_name": "TecnoAll - Ingenieria",
                    },
                    {
                        "employee_name": "Trabajador Compras Prueba",
                        "employee_name_key": "trabajador compras prueba",
                        "group_name": "TecnoAll - Compras",
                    },
                ]
            )

        response = self.client.post(
            "/usuarios/nuevo",
            data={
                "csrf_token": self.csrf_token(),
                "display_name": "José Valdez",
                "username": "jose",
                "password": "UnaClaveSegura123",
                "supervised_area": "",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Usuario creado correctamente", response.data)
        with self.app.app_context():
            jose = get_db().execute(
                "SELECT id, supervised_area FROM users WHERE username = 'jose'"
            ).fetchone()
            self.assertEqual(jose["supervised_area"], "")

        self.client.post(
            "/logout",
            data={"csrf_token": self.csrf_token()},
        )
        self.client.get("/login")
        login = self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "jose",
                "password": "UnaClaveSegura123",
            },
            follow_redirects=True,
        )
        self.assertEqual(login.status_code, 200)

        with patch(
            "app.report_for_week", return_value=([], None, set())
        ), patch("app.local_today", return_value=date(2026, 8, 19)):
            home = self.client.get("/?semana=2026-08-13")

        self.assertIn(b"Trabajador Ingenier", home.data)
        self.assertIn(b"Trabajador Compras Prueba", home.data)
        self.assertIn(b"data-home-preview", home.data)
        self.assertIn(b"Todos los grupos", home.data)
        self.assertIn(b"Asistencia del equipo", home.data)
        self.assertNotIn(b"Supervisor &middot;", home.data)

    def test_worker_user_only_sees_linked_profile_and_home(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            connection = get_db()
            connection.execute(
                """
                UPDATE employees SET area = 'TecnoAll - Ingenieria'
                WHERE employee_name_key = 'ruben humberto lizarraga reyes'
                """
            )
            connection.commit()

        response = self.client.post(
            "/usuarios/nuevo",
            data={
                "csrf_token": self.csrf_token(),
                "display_name": "Rubén Humberto Lizárraga Reyes",
                "username": "trabajador@example.com",
                "password": "UnaClaveTrabajador123",
                "access_role": "worker",
                "employee_name_key": "ruben humberto lizarraga reyes",
                "supervised_area": "TecnoAll - Ingenieria",
            },
            follow_redirects=True,
        )

        self.assertIn(b"Usuario creado correctamente", response.data)
        self.assertIn(b"Usuario: trabajador@example.com", response.data)
        self.assertNotIn(b"@trabajador@example.com", response.data)
        self.assertIn(b"Usuarios administradores registrados", response.data)
        self.assertIn(b"Usuarios TecnoAll - Ingenieria registrados", response.data)
        self.assertEqual(response.data.count(b"data-user-group"), 2)
        self.assertIn(b"data-supervised-area-field", response.data)
        user_script = self.client.get("/static/app.js")
        self.assertIn(b"updateSupervisedAreaVisibility", user_script.data)
        user_script.close()
        with self.app.app_context():
            worker_user = get_db().execute(
                """
                SELECT access_role, employee_name_key, supervised_area
                FROM users WHERE username = 'trabajador@example.com'
                """
            ).fetchone()
            self.assertEqual(worker_user["access_role"], "worker")
            self.assertEqual(
                worker_user["employee_name_key"],
                "ruben humberto lizarraga reyes",
            )
            self.assertEqual(worker_user["supervised_area"], "")

        self.client.post(
            "/logout",
            data={"csrf_token": self.csrf_token()},
        )
        self.client.get("/login")
        login = self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "trabajador@example.com",
                "password": "UnaClaveTrabajador123",
            },
        )
        self.assertEqual(login.status_code, 302)

        with patch(
            "app.report_for_week", return_value=([], None, set())
        ), patch("app.local_today", return_value=date(2026, 8, 19)):
            home = self.client.get(
                "/?semana=2026-08-13&trabajador=jorge+rangel+pulido"
            )

        self.assertEqual(home.status_code, 200)
        self.assertIn(b"Ruben Humberto Lizarraga Reyes", home.data)
        self.assertNotIn(b"Jorge Rangel Pulido", home.data)
        self.assertIn(b"Trabajador", home.data)
        self.assertIn(b"home-browser worker-home-browser", home.data)
        self.assertNotIn(b"Expediente Supervisor", home.data)
        self.assertNotIn(b"data-home-preview", home.data)
        self.assertNotIn(b"Habilitar tiempo extra", home.data)
        self.assertNotIn(b'href="/usuarios"', home.data)
        self.assertNotIn(b'href="/vacaciones"', home.data)
        account_page = self.client.get("/cuenta")
        self.assertEqual(account_page.status_code, 200)
        self.assertIn(b"Actualizar acceso", account_page.data)

        for path in (
            "/trabajadores",
            "/usuarios",
            "/vacaciones",
            "/reporte",
        ):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_worker_must_change_temporary_password_before_home(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            connection = get_db()
            connection.execute(
                """
                INSERT INTO users (
                    username, display_name, password_hash, access_role,
                    employee_name_key, must_change_password, created_at,
                    supervised_area
                ) VALUES (?, ?, ?, 'worker', ?, 1, ?, '')
                """,
                (
                    "017",
                    "Ruben Humberto Lizarraga Reyes",
                    generate_password_hash(
                        "123456789", method="pbkdf2:sha256"
                    ),
                    "ruben humberto lizarraga reyes",
                    "2026-08-26T00:00:00Z",
                ),
            )
            connection.commit()
        self.client.post(
            "/logout", data={"csrf_token": self.csrf_token()}
        )
        self.client.get("/login")
        login = self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "017",
                "password": "123456789",
            },
        )
        self.assertEqual(login.headers["Location"], "/cuenta")
        forced_home = self.client.get("/")
        self.assertEqual(forced_home.headers["Location"], "/cuenta")
        account = self.client.get("/cuenta")
        self.assertIn(b"Registra tu correo y crea tu contrase\xc3\xb1a personal", account.data)
        changed = self.client.post(
            "/cuenta",
            data={
                "csrf_token": self.csrf_token(),
                "username": "ruben.trabajador@tecnoall.com",
                "current_password": "123456789",
                "new_password": "NuevaClavePersonal123",
                "password_confirmation": "NuevaClavePersonal123",
            },
        )
        self.assertEqual(changed.headers["Location"], "/")
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.app.app_context():
            worker = get_db().execute(
                "SELECT username, must_change_password FROM users WHERE employee_name_key = ?",
                ("ruben humberto lizarraga reyes",),
            ).fetchone()
            self.assertEqual(worker["username"], "ruben.trabajador@tecnoall.com")
            self.assertEqual(worker["must_change_password"], 0)

    def test_worker_requests_payroll_pdf_without_persistent_storage(self):
        self.initialize_admin()
        with self.app.app_context():
            connection = get_db()
            connection.execute(
                """
                INSERT INTO users (
                    username, display_name, password_hash, access_role,
                    employee_name_key, must_change_password, active,
                    created_at, supervised_area
                ) VALUES (?, ?, ?, 'worker', ?, 0, 1, ?, '')
                """,
                (
                    "ruben.worker@example.com",
                    "Ruben Humberto Lizarraga Reyes",
                    generate_password_hash(
                        "UnaClaveTrabajador123", method="pbkdf2:sha256"
                    ),
                    "ruben humberto lizarraga reyes",
                    "2026-08-26T20:00:00Z",
                ),
            )
            connection.commit()
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "ruben.worker@example.com",
                "password": "UnaClaveTrabajador123",
            },
        )
        page = self.client.get("/recibos-nomina")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Generar y descargar PDF", page.data)
        self.assertNotIn(b"Historial", page.data)
        self.assertNotIn(b"Neto", page.data)

        broker = self.app.extensions["payroll_download_broker"]
        job_id = broker.create(employee_code="017", year=2026, period=32)
        connector = self.app.test_client()
        denied = connector.post(
            "/api/integraciones/contpaqi/recibos/solicitudes/tomar"
        )
        self.assertEqual(denied.status_code, 401)
        claimed = connector.post(
            "/api/integraciones/contpaqi/recibos/solicitudes/tomar",
            headers={"Authorization": "Bearer sync-test-token"},
        )
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.get_json()["employeeCode"], "017")
        delivered = connector.post(
            f"/api/integraciones/contpaqi/recibos/solicitudes/{job_id}/resultado",
            data={"pdf": (io.BytesIO(b"%PDF-1.4\n%%EOF\n"), "recibo.pdf")},
            headers={"Authorization": "Bearer sync-test-token"},
        )
        self.assertEqual(delivered.status_code, 200)
        pdf, error = broker.wait_and_consume(job_id)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(error, "")
        with self.app.app_context():
            tables = {
                row["name"] for row in get_db().execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertNotIn("payroll_receipts", tables)
        self.assertNotIn("payroll_receipt_items", tables)

    def test_worker_vacation_request_supervisor_decision_and_mailbox_badges(self):
        self.initialize_admin()
        self.login()
        self.client.post(
            "/usuarios/nuevo",
            data={
                "csrf_token": self.csrf_token(),
                "display_name": "Administrador Sin Asignación",
                "username": "other-admin",
                "password": "OtraClaveSegura123",
                "access_role": "admin",
                "employee_name_key": "",
                "supervised_area": "",
            },
        )
        create_worker = self.client.post(
            "/usuarios/nuevo",
            data={
                "csrf_token": self.csrf_token(),
                "display_name": "Ruben Humberto Lizarraga Reyes",
                "username": "ruben.worker@example.com",
                "password": "UnaClaveTrabajador123",
                "access_role": "worker",
                "employee_name_key": "ruben humberto lizarraga reyes",
                "supervised_area": "",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Usuario creado correctamente", create_worker.data)
        with self.app.app_context():
            connection = get_db()
            admin_id = connection.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()["id"]
            connection.execute(
                """
                UPDATE employees
                SET vacation_days_available = 9,
                    vacation_balance_as_of = '2026-08-25'
                WHERE employee_name_key = 'ruben humberto lizarraga reyes'
                """
            )
            connection.executemany(
                """
                INSERT INTO employee_vacation_movements (
                    employee_name_key, source_movement_key, concept,
                    registered_date, start_date, end_date, days_taken,
                    days_entitled, balance, balance_as_of, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "ruben humberto lizarraga reyes",
                        "anniversary:1:2026-05-26",
                        "Aniversario laboral",
                        "2026-05-26", None, None, 0, 12, 12,
                        "2026-08-25", "2026-08-25T18:00:00Z",
                    ),
                    (
                        "ruben humberto lizarraga reyes",
                        "vacation:10",
                        "Vacaciones tomadas",
                        "2026-06-19", "2026-06-19", "2026-06-21",
                        3, 0, 9, "2026-08-25", "2026-08-25T18:00:00Z",
                    ),
                ],
            )
            connection.commit()

        self.client.post(
            "/logout", data={"csrf_token": self.csrf_token()}
        )
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "ruben.worker@example.com",
                "password": "UnaClaveTrabajador123",
            },
        )

        request_page = self.client.get("/solicitar-vacaciones")
        self.assertEqual(request_page.status_code, 200)
        self.assertIn(b"Solicitar vacaciones", request_page.data)
        self.assertIn(b"Saldo disponible", request_page.data)
        self.assertIn(b"Estado de vacaciones", request_page.data)
        self.assertIn(b"Aniversario laboral", request_page.data)
        self.assertIn(b"Vacaciones tomadas", request_page.data)
        self.assertIn(b"19/06/2026", request_page.data)
        self.assertIn(b'data-available-days="9.0"', request_page.data)
        self.assertIn(b"Solicitud en captura", request_page.data)
        self.assertNotIn(b"vacation-balance-icon", request_page.data)
        sent = self.client.post(
            "/solicitar-vacaciones",
            data={
                "csrf_token": self.csrf_token(),
                "supervisor_id": str(admin_id),
                "start_date": "2026-09-01",
                "end_date": "2026-09-05",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Tu solicitud fue enviada al supervisor", sent.data)
        self.assertIn(b"Pendiente", sent.data)

        with self.app.app_context():
            connection = get_db()
            vacation_request = connection.execute(
                "SELECT * FROM vacation_requests"
            ).fetchone()
            admin = connection.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()
            self.assertEqual(vacation_request["requested_days"], 5)
            self.assertEqual(vacation_request["status"], "pending")
            self.assertEqual(vacation_request["supervisor_id"], admin["id"])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM vacations"
                ).fetchone()["total"],
                0,
            )
            request_id = vacation_request["id"]

        self.client.post(
            "/logout", data={"csrf_token": self.csrf_token()}
        )
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "other-admin",
                "password": "OtraClaveSegura123",
            },
        )
        denied_decision = self.client.post(
            f"/buzon/solicitudes/{request_id}/decision",
            data={
                "csrf_token": self.csrf_token(),
                "decision": "approved",
            },
        )
        self.assertEqual(denied_decision.status_code, 404)
        self.client.post(
            "/logout", data={"csrf_token": self.csrf_token()}
        )
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "admin",
                "password": "UnaClaveSegura123",
            },
        )
        with patch(
            "app.report_for_week", return_value=([], None, set())
        ):
            supervisor_home = self.client.get("/")
        self.assertIn(b"nav-badge", supervisor_home.data)
        supervisor_mailbox = self.client.get("/buzon")
        self.assertIn(b"Aprobar", supervisor_mailbox.data)
        self.assertIn(b"Rechazar", supervisor_mailbox.data)

        decided = self.client.post(
            f"/buzon/solicitudes/{request_id}/decision",
            data={
                "csrf_token": self.csrf_token(),
                "decision": "approved",
            },
            follow_redirects=True,
        )
        self.assertIn(b"La solicitud fue aprobada", decided.data)
        with self.app.app_context():
            connection = get_db()
            vacation_request = connection.execute(
                "SELECT * FROM vacation_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            self.assertEqual(vacation_request["status"], "approved")
            self.assertEqual(vacation_request["contpaqi_status"], "pending")
            self.assertIsNotNone(vacation_request["responded_at"])
            self.assertEqual(vacation_request["decided_by"], admin["id"])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM vacations"
                ).fetchone()["total"],
                0,
            )

        self.client.post(
            "/logout", data={"csrf_token": self.csrf_token()}
        )
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "ruben.worker@example.com",
                "password": "UnaClaveTrabajador123",
            },
        )
        with patch(
            "app.report_for_week", return_value=([], None, set())
        ):
            worker_home = self.client.get("/")
        self.assertIn(b"nav-badge", worker_home.data)
        worker_mailbox = self.client.get("/buzon")
        self.assertIn(b"Aprobada", worker_mailbox.data)
        self.assertIn(b"Administrador General", worker_mailbox.data)
        deleted = self.client.post(
            f"/buzon/solicitudes/{request_id}/eliminar",
            data={"csrf_token": self.csrf_token()},
            follow_redirects=True,
        )
        self.assertIn(b"El mensaje fue eliminado de tu buz", deleted.data)
        self.assertNotIn(b"Aprobada", deleted.data)
        self.assertIn(b"mailbox-empty", deleted.data)
        stylesheet = self.client.get("/static/app.css")
        self.assertIn(
            b".mailbox-empty { width: 100%; max-width: none;",
            stylesheet.data,
        )
        self.assertIn(
            b".reject-button { color: white;",
            stylesheet.data,
        )
        stylesheet.close()
        with self.app.app_context():
            stored = get_db().execute(
                """
                SELECT status, worker_deleted_at FROM vacation_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
            self.assertEqual(stored["status"], "approved")
            self.assertIsNotNone(stored["worker_deleted_at"])
        with patch(
            "app.report_for_week", return_value=([], None, set())
        ):
            read_home = self.client.get("/")
        self.assertNotIn(b"nav-badge", read_home.data)

        sent_to_all = self.client.post(
            "/solicitar-vacaciones",
            data={
                "csrf_token": self.csrf_token(),
                "supervisor_id": "all",
                "start_date": "2026-09-08",
                "end_date": "2026-09-10",
            },
        )
        self.assertEqual(sent_to_all.status_code, 302)
        with self.app.app_context():
            connection = get_db()
            all_request = connection.execute(
                """
                SELECT id, sent_to_all FROM vacation_requests
                WHERE start_date = '2026-09-08'
                """
            ).fetchone()
            recipient_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM vacation_request_recipients WHERE request_id = ?
                """,
                (all_request["id"],),
            ).fetchone()["total"]
            self.assertEqual(all_request["sent_to_all"], 1)
            self.assertEqual(recipient_count, 2)

        self.client.post(
            "/logout", data={"csrf_token": self.csrf_token()}
        )
        self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "username": "other-admin",
                "password": "OtraClaveSegura123",
            },
        )
        other_mailbox = self.client.get("/buzon")
        self.assertIn(b"Todos los supervisores", other_mailbox.data)
        rejected = self.client.post(
            f"/buzon/solicitudes/{all_request['id']}/decision",
            data={
                "csrf_token": self.csrf_token(),
                "decision": "rejected",
            },
            follow_redirects=True,
        )
        self.assertIn(b"La solicitud fue rechazada", rejected.data)

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
        self.assertIn(b"Expediente Supervisor", response.data)
        self.assertIn(b"Expediente Contador", response.data)
        self.assertIn(b'/expediente-contador.xlsx', response.data)
        self.assertIn(b"Grupo para el reporte", response.data)
        self.assertNotIn(b"Actualizar datos", response.data)

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
        self.assertIn(b"Correo o usuario", response.data)
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

    def test_workers_use_hikvision_groups_and_known_employee_codes(self):
        self.initialize_admin()
        self.login()

        with self.app.app_context():
            save_employees(
                [{
                    "employee_name": "Jorge Rangel Pulido",
                    "employee_name_key": "jorge rangel pulido",
                    "group_name": "TecnoAll - Ingenieria",
                }, {
                    "employee_name": "Ana López",
                    "employee_name_key": "ana lopez",
                    "group_name": "TecnoAll - Compras",
                }, {
                    "employee_name": "Persona Inactiva",
                    "employee_name_key": "persona inactiva",
                    "group_name": "TecnoAll - Bajas e Inactivos",
                }]
            )

        response = self.client.get("/trabajadores")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ana López".encode(), response.data)
        self.assertIn(b"TecnoAll - Ingenieria", response.data)
        self.assertIn(b"TecnoAll - Compras", response.data)
        self.assertIn(b"Persona Inactiva", response.data)
        self.assertIn(b"TecnoAll - Bajas e Inactivos", response.data)
        self.assertIn(b"063", response.data)
        self.assertNotIn(b"Guardar todas las \xc3\xa1reas", response.data)
        self.assertNotIn(b'<select id="area-', response.data)
        self.assertIn(b"data-employees-area-filter", response.data)
        self.assertIn(
            b'data-employee-area="tecnoall - ingenieria"',
            response.data,
        )

        response = self.client.post(
            "/trabajadores/areas",
            data={
                "csrf_token": self.csrf_token(),
            },
        )
        self.assertEqual(response.status_code, 404)

        with self.app.app_context():
            save_employees(
                [{
                    "employee_name": "RANGEL PULIDO JORGE",
                    "employee_name_key": "jorge rangel pulido",
                    "group_name": "TecnoAll - Ventas Tijuana",
                }]
            )
            employee = get_db().execute(
                """
                SELECT employee_name, employee_code, area FROM employees
                WHERE employee_name_key = 'jorge rangel pulido'
                """
            ).fetchone()
            self.assertEqual(employee["employee_name"], "RANGEL PULIDO JORGE")
            self.assertEqual(employee["employee_code"], "063")
            self.assertEqual(employee["area"], "TecnoAll - Ventas Tijuana")

        with patch("app.cached_attendance", return_value=[]):
            response = self.client.get("/?semana=2026-07-23")
        self.assertIn(
            b'data-worker-area="tecnoall - ventas tijuana"',
            response.data,
        )
        self.assertIn(b'data-worker-code="063"', response.data)
        self.assertIn(b"data-home-area-filter", response.data)
        self.assertNotIn(b"Persona Inactiva", response.data)
        self.assertNotIn(b"TecnoAll - Bajas e Inactivos", response.data)

    def test_vacations_are_saved_and_suppress_missing_punch_alerts(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            save_employees(
                [{
                    "employee_name": "Jorge Rangel Pulido",
                    "employee_name_key": "jorge rangel pulido",
                    "group_name": "TecnoAll - Ingenieria",
                }]
            )

        page = self.client.get(
            "/vacaciones?grupo=TecnoAll%20-%20Ingenieria"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Registrar vacaciones", page.data)
        self.assertIn(b"data-vacation-worker", page.data)

        response = self.client.post(
            "/vacaciones/nueva",
            data={
                "csrf_token": self.csrf_token(),
                "employee_name_key": "jorge rangel pulido",
                "group_name": "TecnoAll - Ingenieria",
                "start_date": "2026-08-13",
                "end_date": "2026-08-16",
            },
            follow_redirects=True,
        )
        self.assertIn(
            "periodo de vacaciones fue guardado".encode(),
            response.data,
        )
        with self.app.app_context():
            vacation = get_db().execute(
                "SELECT * FROM vacations"
            ).fetchone()
            calendar, _days = weekly_report_calendar(
                [],
                date(2026, 8, 13),
                [vacation],
            )
            worker = next(
                item for item in calendar
                if item["employee_name_key"] == "jorge rangel pulido"
            )
            self.assertTrue(worker["cells"][0]["is_vacation"])
            self.assertFalse(worker["cells"][0]["is_incomplete"])
            self.assertTrue(worker["cells"][2]["is_vacation"])
            self.assertFalse(worker["cells"][3]["is_vacation"])
            self.assertTrue(worker["cells"][3]["is_non_working"])

        with (
            patch("app.report_for_week", return_value=([], "", set())),
            patch("app.local_today", return_value=date(2026, 8, 18)),
        ):
            home = self.client.get(
                "/?semana=2026-08-13&trabajador=jorge%20rangel%20pulido"
            )
            report = self.client.get("/reporte?semana=2026-08-13")
        self.assertIn(b"home-day-card vacation", home.data)
        self.assertIn(b"Vacaciones autorizadas", home.data)
        self.assertIn(b"report-day-cell\n                             vacation", report.data)
        self.assertIn(b"Vacaciones", report.data)

        stylesheet = self.client.get("/static/app.css")
        self.assertIn(b".is-filtered-out", stylesheet.data)
        stylesheet.close()

    def test_vacations_page_separates_active_periods_from_history(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            save_employees([{
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "group_name": "TecnoAll - Ingenieria",
            }])
            connection = get_db()
            user_id = connection.execute(
                "SELECT id FROM users LIMIT 1"
            ).fetchone()["id"]
            connection.executemany(
                """
                INSERT INTO vacations (
                    employee_name, employee_name_key, start_date, end_date,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("Periodo Pasado", "jorge rangel pulido", "2026-08-01", "2026-08-05", user_id, "2026-07-01"),
                    ("Periodo Vigente", "jorge rangel pulido", "2026-08-20", "2026-08-27", user_id, "2026-07-02"),
                    ("Periodo Futuro", "jorge rangel pulido", "2026-09-01", "2026-09-05", user_id, "2026-07-03"),
                ],
            )
            connection.commit()

        with patch("app.local_today", return_value=date(2026, 8, 25)):
            response = self.client.get(
                "/vacaciones?grupo=TecnoAll%20-%20Ingenieria"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Periodos vacacionales activos", response.data)
        active_section, history_section = response.data.split(
            b'<details class="vacation-history"', 1
        )
        self.assertNotIn(b"Periodo Pasado", active_section)
        self.assertIn(b"Periodo Vigente", active_section)
        self.assertIn(b"Periodo Futuro", active_section)
        self.assertIn(b"Periodo Pasado", history_section)
        self.assertNotIn(b"Periodo Vigente", history_section)
        self.assertIn(b"data-vacation-history-search", history_section)
        self.assertIn(b'data-vacation-history-row', history_section)
        self.assertIn(b'<article class="vacation-record" hidden', history_section)
        self.assertIn(b'data-history-name="Periodo Pasado"', history_section)
        self.assertNotIn(b"data-history-search", history_section)

    def test_incident_permission_checkbox_is_persisted(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            save_employees([{
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "group_name": "TecnoAll - Ingenieria",
            }])

        response = self.client.post(
            "/permisos-incidencia",
            data={
                "csrf_token": self.csrf_token(),
                "employee_name_key": "jorge rangel pulido",
                "work_date": "2026-08-17",
                "has_permission": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            permission = get_db().execute(
                "SELECT * FROM attendance_permissions"
            ).fetchone()
            self.assertEqual(permission["work_date"], "2026-08-17")

        with (
            patch("app.report_for_week", return_value=([], "", set())),
            patch("app.local_today", return_value=date(2026, 8, 18)),
        ):
            home = self.client.get(
                "/?semana=2026-08-13&trabajador=jorge%20rangel%20pulido"
            )
        self.assertIn(b'data-work-date="2026-08-17"', home.data)
        self.assertIn(b"Permiso aplicado", home.data)
        self.assertIn(b"checked", home.data)

        response = self.client.post(
            "/permisos-incidencia",
            data={
                "csrf_token": self.csrf_token(),
                "employee_name_key": "jorge rangel pulido",
                "work_date": "2026-08-17",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            remaining = get_db().execute(
                "SELECT COUNT(*) AS total FROM attendance_permissions"
            ).fetchone()["total"]
        self.assertEqual(remaining, 0)

    def test_excel_records_are_filtered_by_group_with_incidents(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            save_employees(
                [
                    {
                        "employee_name": "Jorge Rangel Pulido",
                        "employee_name_key": "jorge rangel pulido",
                        "group_name": "TecnoAll - Ingenieria",
                    },
                    {
                        "employee_name": "Ana López",
                        "employee_name_key": "ana lopez",
                        "group_name": "TecnoAll - Compras",
                    },
                    {
                        "employee_name": "Persona Inactiva",
                        "employee_name_key": "persona inactiva",
                        "group_name": "TecnoAll - Bajas e Inactivos",
                    },
                ]
            )
        report_rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 23),
                "clock_in": time(8, 20),
                "clock_out": None,
                "approved_minutes": 120,
                "authorized_minutes": 60,
            }
        ]
        with patch(
            "app.report_for_week",
            return_value=(report_rows, None, {date(2026, 7, 23)}),
        ), patch("app.local_today", return_value=date(2026, 7, 29)):
            response = self.client.get(
                "/expedientes.xlsx",
                query_string={
                    "semana": "2026-07-23",
                    "grupo": "TecnoAll - Ingenieria",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(b"expedientes_2026-07-23.xlsx", response.headers[
            "Content-Disposition"
        ].encode())
        with zipfile.ZipFile(io.BytesIO(response.data)) as workbook:
            strings = workbook.read("xl/sharedStrings.xml").decode("utf-8")
            styles = workbook.read("xl/styles.xml").decode("utf-8")
        self.assertIn("Jorge Rangel Pulido", strings)
        self.assertNotIn("Ana López", strings)
        self.assertNotIn("Persona Inactiva", strings)
        self.assertIn("Llegada tarde · No checó salida", strings)
        self.assertIn("Ausencia a laborar", strings)
        self.assertIn("FFC62828", styles)

    def test_accountant_excel_has_seven_days_and_overtime_codes(self):
        self.initialize_admin()
        self.login()
        with self.app.app_context():
            save_employees(
                [
                    {
                        "employee_name": "Jorge Rangel Pulido",
                        "employee_name_key": "jorge rangel pulido",
                        "group_name": "TecnoAll - Ingenieria",
                    },
                    {
                        "employee_name": "Ana López",
                        "employee_name_key": "ana lopez",
                        "group_name": "TecnoAll - Compras",
                    },
                ]
            )
        report_rows = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 23),
                "authorized_minutes": 480,
            },
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": date(2026, 7, 24),
                "authorized_minutes": 180,
            },
        ]
        with patch(
            "app.report_for_week",
            return_value=(report_rows, None, {date(2026, 7, 23)}),
        ):
            response = self.client.get(
                "/expediente-contador.xlsx",
                query_string={
                    "semana": "2026-07-23",
                    "grupo": "TecnoAll - Ingenieria",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"expediente_contador_2026-07-23.xlsx",
            response.headers["Content-Disposition"].encode(),
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as workbook:
            strings = workbook.read("xl/sharedStrings.xml").decode("utf-8")
        self.assertIn("Jorge Rangel Pulido", strings)
        self.assertNotIn("Ana López", strings)
        self.assertIn("Jue 23/07", strings)
        self.assertIn("Mié 29/07", strings)
        self.assertIn("8HE2", strings)
        self.assertIn("1HE2 / 2HE3", strings)

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
