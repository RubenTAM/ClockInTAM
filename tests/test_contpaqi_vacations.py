import unittest
from datetime import date, datetime
from decimal import Decimal

from nomina.contpaqi_vacations import (
    BenefitRow,
    ContpaqiVacationError,
    calculate_available_days,
)


class ContpaqiVacationCalculationTests(unittest.TestCase):
    def test_calculates_completed_anniversaries_and_subtracts_taken_days(self):
        employee = {
            "FechaAlta": datetime(2023, 5, 10),
            "FechaReingreso": None,
            "TipoPrestacion": 1,
            "DiasVacTomadasAntesdeAlta": 1,
        }
        benefits = [
            BenefitRow(1, 1, Decimal("12"), date(2023, 1, 1)),
            BenefitRow(1, 2, Decimal("14"), date(2023, 1, 1)),
            BenefitRow(1, 3, Decimal("16"), date(2023, 1, 1)),
        ]

        result = calculate_available_days(
            employee,
            benefits,
            vacation_days_taken=Decimal("5"),
            as_of=date(2026, 5, 9),
        )

        self.assertEqual(result, Decimal("20.00"))

    def test_uses_benefit_rule_effective_on_each_anniversary(self):
        employee = {
            "FechaAlta": date(2021, 6, 1),
            "FechaReingreso": None,
            "TipoPrestacion": 7,
            "DiasVacTomadasAntesdeAlta": 0,
        }
        benefits = [
            BenefitRow(7, 1, Decimal("6"), date(1970, 1, 1)),
            BenefitRow(7, 2, Decimal("8"), date(1970, 1, 1)),
            BenefitRow(7, 1, Decimal("12"), date(2023, 1, 1)),
            BenefitRow(7, 2, Decimal("14"), date(2023, 1, 1)),
            BenefitRow(7, 3, Decimal("16"), date(2023, 1, 1)),
        ]

        result = calculate_available_days(
            employee,
            benefits,
            vacation_days_taken=0,
            as_of=date(2024, 6, 1),
        )

        self.assertEqual(result, Decimal("36.00"))

    def test_rejects_employee_without_benefit_table(self):
        employee = {
            "FechaAlta": date(2020, 1, 1),
            "FechaReingreso": None,
            "TipoPrestacion": 99,
        }

        with self.assertRaises(ContpaqiVacationError):
            calculate_available_days(
                employee, [], vacation_days_taken=0, as_of=date(2026, 1, 1)
            )


if __name__ == "__main__":
    unittest.main()
