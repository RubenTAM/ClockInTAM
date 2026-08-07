import unittest
from datetime import date, time

from excel_reports import (
    attendance_incidents,
    build_accountant_rows,
    round_overtime_code_hours,
)


class ExcelReportTests(unittest.TestCase):
    def test_overtime_codes_round_up_only_from_fifty_minutes(self):
        self.assertEqual(round_overtime_code_hours(49), 0)
        self.assertEqual(round_overtime_code_hours(50), 1)
        self.assertEqual(round_overtime_code_hours(60), 1)
        self.assertEqual(round_overtime_code_hours(109), 1)
        self.assertEqual(round_overtime_code_hours(110), 2)

    def test_accountant_rows_split_weekly_double_and_triple_hours(self):
        employee = {
            "employee_code": "009",
            "employee_name": "José Valdez",
            "employee_name_key": "jose valdez",
            "area": "TecnoAll - Ingeniería",
        }
        report_rows = [
            {
                "employee_name_key": "jose valdez",
                "work_date": date(2026, 8, 6),
                "authorized_minutes": 480,
            },
            {
                "employee_name_key": "jose valdez",
                "work_date": date(2026, 8, 7),
                "authorized_minutes": 180,
            },
        ]

        rows = build_accountant_rows(
            report_rows,
            [employee],
            date(2026, 8, 6),
            "TecnoAll - Ingeniería",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["day_codes"][0], "8HE2")
        self.assertEqual(rows[0]["day_codes"][1], "1HE2 / 2HE3")
        self.assertEqual(rows[0]["day_codes"][2:], ["", "", "", "", ""])

    def test_attendance_incidents_cover_missing_punches_and_lateness(self):
        monday = date(2026, 8, 10)
        saturday = date(2026, 8, 8)

        self.assertEqual(
            attendance_incidents(monday, None, None),
            "Ausencia a laborar",
        )
        self.assertEqual(
            attendance_incidents(monday, None, time(17, 0)),
            "No checó entrada",
        )
        self.assertEqual(
            attendance_incidents(monday, time(8, 20), None),
            "No checó salida · Llegada tarde",
        )
        self.assertEqual(
            attendance_incidents(saturday, time(8, 50), time(13, 0)),
            "Llegada tarde",
        )
        self.assertEqual(
            attendance_incidents(monday, time(8, 19), time(17, 0)),
            "",
        )
        self.assertEqual(
            attendance_incidents(
                monday,
                time(8, 20),
                None,
                day_complete=False,
            ),
            "Llegada tarde",
        )


if __name__ == "__main__":
    unittest.main()
