from datetime import date, time
import unittest

from reporting import (
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
                }
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["clock_in"], time(6, 0))
        self.assertEqual(rows[0]["clock_out"], time(20, 0))
        self.assertEqual(rows[0]["overtime_minutes"], 180)

    def test_weekday_overtime_uses_company_schedule(self):
        self.assertEqual(
            scheduled_overtime_minutes(
                date(2026, 7, 13),
                time(6, 58),
                time(17, 2),
            ),
            2,
        )

    def test_saturday_overtime_starts_at_one_pm(self):
        self.assertEqual(
            scheduled_overtime_minutes(
                date(2026, 7, 18),
                time(8, 10),
                time(14, 6),
            ),
            66,
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
        self.assertEqual(result["unauthorized_minutes"], 60)
        self.assertEqual(result["status_key"], "exceeded")

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

    def test_week_report_omits_normal_days(self):
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
        self.assertEqual(build_weekly_report(attendance, []), [])


if __name__ == "__main__":
    unittest.main()
