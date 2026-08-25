from datetime import date, time
import unittest

from checador.reporting import (
    build_daily_attendance,
    build_weekly_report,
    compare_overtime,
    normalize_name,
    scheduled_overtime_minutes,
)


class ReportingTests(unittest.TestCase):
    def test_normalizes_names_for_matching(self):
        self.assertEqual(
            normalize_name("  MARIO  Ángel Hernández "),
            "mario angel hernandez",
        )

    def test_builds_daily_attendance_from_hik_report(self):
        rows = build_daily_attendance(
            [
                {
                    "fullName": "Mario Ángel Hernández",
                    "clockInDate": "2026/07/22",
                    "clockInTime": "06:00",
                    "clockOutDate": "2026/07/22",
                    "clockOutTime": "20:00",
                    "overtimeDuration": "03:00",
                    "clockOutArea": "Tijuana",
                    "groupName": "TecnoAll - Ingenieria",
                }
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["clock_in"], time(6, 0))
        self.assertEqual(rows[0]["clock_out"], time(20, 0))
        self.assertEqual(rows[0]["overtime_minutes"], 300)
        self.assertEqual(rows[0]["group_name"], "TecnoAll - Ingenieria")

    def test_weekday_overtime_uses_company_schedule(self):
        self.assertEqual(
            scheduled_overtime_minutes(
                date(2026, 7, 13),
                time(6, 58),
                time(17, 2),
            ),
            64,
        )

    def test_saturday_overtime_starts_at_one_pm(self):
        self.assertEqual(
            scheduled_overtime_minutes(
                date(2026, 7, 18),
                time(8, 10),
                time(14, 6),
            ),
            86,
        )

    def test_sunday_counts_the_complete_worked_interval(self):
        self.assertEqual(
            scheduled_overtime_minutes(
                date(2026, 7, 19),
                time(14, 57),
                time(22, 4),
            ),
            427,
        )

    def test_early_authorization_matches_time_before_shift(self):
        attendance = build_daily_attendance(
            [
                {
                    "fullName": "Jorge Rangel Pulido",
                    "clockInDate": "2026/07/22",
                    "clockInTime": "06:52",
                    "clockOutDate": "2026/07/22",
                    "clockOutTime": "17:02",
                    "overtimeDuration": "01:08",
                }
            ]
        )
        authorization = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": "2026-07-22",
                "allowed_start": "07:00",
                "allowed_end": "08:00",
                "note": "",
            }
        ]
        result = build_weekly_report(attendance, authorization)[0]
        self.assertEqual(result["authorized_minutes"], 60)
        self.assertEqual(result["unauthorized_minutes"], 10)
        self.assertEqual(result["status_key"], "exceeded")

    def test_separates_authorized_and_excess_minutes(self):
        attendance = {
            "employee_name": "Mario Ángel Hernández",
            "employee_name_key": "mario angel hernandez",
            "work_date": date(2026, 7, 22),
            "clock_in": time(7, 0),
            "clock_out": time(20, 0),
            "overtime_minutes": 180,
        }
        authorization = {
            "employee_name": "Mario Angel Hernandez",
            "employee_name_key": "mario angel hernandez",
            "work_date": "2026-07-22",
            "allowed_start": "17:00",
            "allowed_end": "19:00",
            "note": "",
        }
        result = compare_overtime(attendance, authorization)
        self.assertEqual(result["actual_range"], "17:00–20:00")
        self.assertEqual(result["authorized_minutes"], 120)
        self.assertEqual(result["approved_minutes"], 120)
        self.assertEqual(result["unauthorized_minutes"], 60)
        self.assertEqual(result["status_key"], "exceeded")

    def test_approved_minutes_limit_the_authorized_window(self):
        attendance = {
            "employee_name": "Mario Ángel Hernández",
            "employee_name_key": "mario angel hernandez",
            "work_date": date(2026, 7, 22),
            "clock_in": time(8, 0),
            "clock_out": time(21, 0),
            "overtime_minutes": 240,
        }
        authorization = {
            "employee_name": "Mario Angel Hernandez",
            "employee_name_key": "mario angel hernandez",
            "work_date": "2026-07-22",
            "allowed_start": "17:00",
            "allowed_end": "21:00",
            "approved_minutes": 120,
            "note": "",
        }

        result = compare_overtime(attendance, authorization)

        self.assertEqual(result["authorized_minutes"], 120)
        self.assertEqual(result["unauthorized_minutes"], 120)
        self.assertEqual(result["unused_minutes"], 0)
        self.assertEqual(result["status_key"], "exceeded")

    def test_limit_applies_across_early_and_late_overtime(self):
        attendance = build_daily_attendance(
            [
                {
                    "fullName": "Andrés Lizárraga",
                    "clockInDate": "2026/07/27",
                    "clockInTime": "07:00",
                    "clockOutDate": "2026/07/27",
                    "clockOutTime": "19:00",
                }
            ]
        )
        authorization = {
            "employee_name": "Andrés Lizárraga",
            "employee_name_key": "andres lizarraga",
            "work_date": "2026-07-27",
            "allowed_start": "07:00",
            "allowed_end": "19:00",
            "approved_minutes": 120,
            "note": "",
        }

        result = compare_overtime(attendance[0], authorization)

        self.assertEqual(result["overtime_minutes"], 180)
        self.assertEqual(result["actual_range"], "07:00–08:00, 17:00–19:00")
        self.assertEqual(result["approved_minutes"], 120)
        self.assertEqual(result["authorized_minutes"], 120)
        self.assertEqual(result["unauthorized_minutes"], 60)

    def test_combines_multiple_authorized_intervals_without_duplicates(self):
        attendance = build_daily_attendance(
            [
                {
                    "fullName": "Jorge Rangel Pulido",
                    "clockInDate": "2026/07/27",
                    "clockInTime": "07:00",
                    "clockOutDate": "2026/07/27",
                    "clockOutTime": "19:00",
                }
            ]
        )
        authorizations = [
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": "2026-07-27",
                "allowed_start": "07:00",
                "allowed_end": "08:00",
                "note": "",
            },
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": "2026-07-27",
                "allowed_start": "17:00",
                "allowed_end": "19:00",
                "note": "",
            },
            {
                "employee_name": "Jorge Rangel Pulido",
                "employee_name_key": "jorge rangel pulido",
                "work_date": "2026-07-27",
                "allowed_start": "17:30",
                "allowed_end": "18:30",
                "note": "",
            },
        ]
        result = build_weekly_report(attendance, authorizations)[0]
        self.assertEqual(result["allowed_range"], "07:00–08:00, 17:00–19:00")
        self.assertEqual(result["authorized_minutes"], 180)
        self.assertEqual(result["unauthorized_minutes"], 0)
        self.assertEqual(result["status_key"], "authorized")

    def test_marks_overtime_without_permission(self):
        attendance = {
            "employee_name": "Jorge Rangel",
            "employee_name_key": "jorge rangel",
            "work_date": date(2026, 7, 23),
            "clock_in": time(7, 0),
            "clock_out": time(18, 0),
            "overtime_minutes": 60,
        }
        result = compare_overtime(attendance, None)
        self.assertEqual(result["authorized_minutes"], 0)
        self.assertEqual(result["unauthorized_minutes"], 60)
        self.assertEqual(result["status_key"], "unauthorized")

    def test_marks_unused_authorization(self):
        authorization = {
            "employee_name": "Salvador Márquez",
            "employee_name_key": "salvador marquez",
            "work_date": "2026-07-24",
            "allowed_start": "17:00",
            "allowed_end": "19:00",
            "note": "",
        }
        result = compare_overtime(None, authorization)
        self.assertEqual(result["unused_minutes"], 120)
        self.assertEqual(result["status_key"], "unused")

    def test_week_report_includes_normal_attendance(self):
        attendance = [
            {
                "employee_name": "Persona sin extra",
                "employee_name_key": "persona sin extra",
                "work_date": date(2026, 7, 20),
                "clock_in": time(8, 0),
                "clock_out": time(17, 0),
                "overtime_minutes": 0,
            }
        ]
        result = build_weekly_report(attendance, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status_key"], "normal")


if __name__ == "__main__":
    unittest.main()
