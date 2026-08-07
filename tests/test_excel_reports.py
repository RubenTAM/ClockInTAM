import unittest
from datetime import date, time

from excel_reports import attendance_incidents


class ExcelReportTests(unittest.TestCase):
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
