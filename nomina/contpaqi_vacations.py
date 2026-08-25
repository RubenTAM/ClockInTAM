from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

import pymssql


class ContpaqiVacationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BenefitRow:
    table_id: int
    seniority: int
    vacation_days: Decimal
    effective_from: date


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _anniversary(start: date, years: int) -> date:
    try:
        return start.replace(year=start.year + years)
    except ValueError:
        return start.replace(year=start.year + years, day=28)


def seniority_date(employee: dict) -> date:
    hired_at = _as_date(employee.get("FechaAlta"))
    rehired_at = _as_date(employee.get("FechaReingreso"))
    if hired_at is None:
        raise ContpaqiVacationError("El empleado no tiene fecha de alta.")
    if rehired_at and rehired_at.year >= 1900 and rehired_at >= hired_at:
        return rehired_at
    return hired_at


def calculate_available_days(
    employee: dict,
    benefit_rows: Iterable[BenefitRow],
    vacation_days_taken: Decimal | int | float,
    as_of: date,
) -> Decimal:
    """Calculate completed-anniversary vacation balance.

    This mirrors the conservative CONTPAQi report mode that excludes the
    proportional portion of the anniversary currently in progress.
    """
    started_at = seniority_date(employee)
    completed_years = as_of.year - started_at.year
    if _anniversary(started_at, completed_years) > as_of:
        completed_years -= 1
    completed_years = max(0, completed_years)

    table_id = int(employee.get("TipoPrestacion") or 0)
    applicable_rows = [
        row for row in benefit_rows if row.table_id == table_id
    ]
    if completed_years and not applicable_rows:
        raise ContpaqiVacationError(
            "No se encontró la tabla de prestaciones del empleado."
        )

    earned = Decimal("0")
    for year_number in range(1, completed_years + 1):
        anniversary = _anniversary(started_at, year_number)
        candidates = [
            row
            for row in applicable_rows
            if row.seniority <= year_number
            and row.effective_from <= anniversary
        ]
        if not candidates:
            raise ContpaqiVacationError(
                f"No hay prestación vigente para la antigüedad {year_number}."
            )
        selected = max(
            candidates,
            key=lambda row: (row.seniority, row.effective_from),
        )
        earned += selected.vacation_days

    taken_before_registration = Decimal(
        str(employee.get("DiasVacTomadasAntesdeAlta") or 0)
    )
    available = (
        earned
        - taken_before_registration
        - Decimal(str(vacation_days_taken or 0))
    )
    return available.quantize(Decimal("0.01"))


def read_vacation_balances(
    *,
    server: str,
    port: int,
    database: str,
    username: str,
    password: str,
    as_of: date,
) -> tuple[list[dict], list[dict]]:
    """Read CONTPAQi tables and return balances plus per-employee errors."""
    try:
        connection = pymssql.connect(
            server=server,
            port=port,
            user=username,
            password=password,
            database=database,
            login_timeout=8,
            timeout=30,
            autocommit=False,
        )
    except Exception as exc:
        raise ContpaqiVacationError(
            "No fue posible conectarse a CONTPAQi Nóminas."
        ) from exc

    try:
        cursor = connection.cursor(as_dict=True)
        cursor.execute("SET LOCK_TIMEOUT 5000")
        cursor.execute(
            """
            SELECT
                IdEmpleado, CodigoEmpleado, NombreLargo, EstadoEmpleado,
                FechaAlta, FechaReingreso, TipoPrestacion,
                DiasVacTomadasAntesdeAlta
            FROM NOM10001
            WHERE LTRIM(RTRIM(CodigoEmpleado)) <> ''
            """
        )
        employees = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                IdTablaPrestacion, Antiguedad, DiasVacaciones,
                FechaInicioVigencia
            FROM NOM10051
            """
        )
        benefit_rows = [
            BenefitRow(
                table_id=int(row["IdTablaPrestacion"]),
                seniority=int(row["Antiguedad"]),
                vacation_days=Decimal(str(row["DiasVacaciones"] or 0)),
                effective_from=_as_date(row["FechaInicioVigencia"])
                or date(1970, 1, 1),
            )
            for row in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT IdEmpleado, SUM(CONVERT(float, DiasVacaciones)) AS DiasTomados
            FROM NOM10014
            GROUP BY IdEmpleado
            """
        )
        taken_by_employee = {
            int(row["IdEmpleado"]): Decimal(str(row["DiasTomados"] or 0))
            for row in cursor.fetchall()
        }
        connection.rollback()
    except Exception as exc:
        connection.rollback()
        raise ContpaqiVacationError(
            "No fue posible consultar las tablas de vacaciones de CONTPAQi."
        ) from exc
    finally:
        connection.close()

    balances = []
    errors = []
    for employee in employees:
        code = str(employee["CodigoEmpleado"] or "").strip()
        try:
            available = calculate_available_days(
                employee,
                benefit_rows,
                taken_by_employee.get(int(employee["IdEmpleado"]), 0),
                as_of,
            )
        except (ContpaqiVacationError, TypeError, ValueError) as exc:
            errors.append({"employeeCode": code, "error": str(exc)})
            continue
        balances.append(
            {
                "employeeId": int(employee["IdEmpleado"]),
                "employeeCode": code,
                "employeeName": str(employee.get("NombreLargo") or "").strip(),
                "employeeStatus": str(
                    employee.get("EstadoEmpleado") or ""
                ).strip(),
                "availableDays": float(available),
                "asOf": as_of.isoformat(),
            }
        )
    return balances, errors
