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


def _completed_years(started_at: date, as_of: date) -> int:
    years = as_of.year - started_at.year
    if _anniversary(started_at, years) > as_of:
        years -= 1
    return max(0, years)


def _benefit_for_anniversary(
    benefit_rows: Iterable[BenefitRow],
    table_id: int,
    year_number: int,
    anniversary: date,
) -> BenefitRow:
    candidates = [
        row
        for row in benefit_rows
        if row.table_id == table_id
        and row.seniority <= year_number
        and row.effective_from <= anniversary
    ]
    if not candidates:
        raise ContpaqiVacationError(
            f"No hay prestación vigente para la antigüedad {year_number}."
        )
    return max(
        candidates,
        key=lambda row: (row.seniority, row.effective_from),
    )


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
    completed_years = _completed_years(started_at, as_of)

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
        selected = _benefit_for_anniversary(
            applicable_rows,
            table_id,
            year_number,
            anniversary,
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


def build_vacation_ledger(
    employee: dict,
    benefit_rows: Iterable[BenefitRow],
    vacation_rows: Iterable[dict],
    as_of: date,
) -> list[dict]:
    """Build the CONTPAQi-style vacation statement for one employee."""
    benefit_rows = list(benefit_rows)
    started_at = seniority_date(employee)
    completed_years = _completed_years(started_at, as_of)
    table_id = int(employee.get("TipoPrestacion") or 0)
    if completed_years and not any(
        row.table_id == table_id for row in benefit_rows
    ):
        raise ContpaqiVacationError(
            "No se encontró la tabla de prestaciones del empleado."
        )

    events = []
    taken_before = Decimal(
        str(employee.get("DiasVacTomadasAntesdeAlta") or 0)
    )
    events.append(
        {
            "sourceMovementKey": "before-registration",
            "concept": "Vac. tomadas antes del registro",
            "sortDate": date.min,
            "sortOrder": 0,
            "registeredDate": None,
            "startDate": None,
            "endDate": None,
            "daysTaken": taken_before,
            "daysEntitled": Decimal("0"),
        }
    )

    for year_number in range(1, completed_years + 1):
        anniversary = _anniversary(started_at, year_number)
        benefit = _benefit_for_anniversary(
            benefit_rows,
            table_id,
            year_number,
            anniversary,
        )
        events.append(
            {
                "sourceMovementKey": (
                    f"anniversary:{year_number}:{anniversary.isoformat()}"
                ),
                "concept": "Aniversario laboral",
                "sortDate": anniversary,
                "sortOrder": 1,
                "registeredDate": anniversary,
                "startDate": None,
                "endDate": None,
                "daysTaken": Decimal("0"),
                "daysEntitled": benefit.vacation_days,
            }
        )

    for index, row in enumerate(vacation_rows):
        start_date = _as_date(row.get("FechaInicio"))
        end_date = _as_date(row.get("FechaFin"))
        registered_date = _as_date(row.get("TimeStamp")) or start_date
        movement_id = row.get("IdTControlVacaciones")
        events.append(
            {
                "sourceMovementKey": (
                    f"vacation:{movement_id}"
                    if movement_id is not None
                    else f"vacation-row:{index}"
                ),
                "concept": "Vacaciones tomadas",
                "sortDate": start_date or registered_date or date.max,
                "sortOrder": 2,
                "registeredDate": registered_date,
                "startDate": start_date,
                "endDate": end_date,
                "daysTaken": Decimal(str(row.get("DiasVacaciones") or 0)),
                "daysEntitled": Decimal("0"),
            }
        )

    balance = Decimal("0")
    ledger = []
    for event in sorted(
        events,
        key=lambda item: (item["sortDate"], item["sortOrder"]),
    ):
        balance += event["daysEntitled"] - event["daysTaken"]
        ledger.append(
            {
                "sourceMovementKey": event["sourceMovementKey"],
                "concept": event["concept"],
                "registeredDate": (
                    event["registeredDate"].isoformat()
                    if event["registeredDate"]
                    else None
                ),
                "startDate": (
                    event["startDate"].isoformat()
                    if event["startDate"]
                    else None
                ),
                "endDate": (
                    event["endDate"].isoformat()
                    if event["endDate"]
                    else None
                ),
                "daysTaken": float(event["daysTaken"]),
                "daysEntitled": float(event["daysEntitled"]),
                "balance": float(balance.quantize(Decimal("0.01"))),
            }
        )
    latest_anniversary = next(
        (
            index
            for index in range(len(ledger) - 1, -1, -1)
            if ledger[index]["concept"] == "Aniversario laboral"
        ),
        None,
    )
    return ledger[latest_anniversary:] if latest_anniversary is not None else ledger


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
            SELECT
                IdTControlVacaciones, IdEmpleado, DiasVacaciones,
                FechaInicio, FechaFin, [TimeStamp]
            FROM NOM10014
            ORDER BY IdEmpleado, FechaInicio, IdTControlVacaciones
            """
        )
        vacations_by_employee: dict[int, list[dict]] = {}
        for row in cursor.fetchall():
            vacations_by_employee.setdefault(
                int(row["IdEmpleado"]), []
            ).append(row)
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
        vacation_rows = vacations_by_employee.get(
            int(employee["IdEmpleado"]), []
        )
        vacation_days_taken = sum(
            (
                Decimal(str(row["DiasVacaciones"] or 0))
                for row in vacation_rows
            ),
            Decimal("0"),
        )
        try:
            available = calculate_available_days(
                employee,
                benefit_rows,
                vacation_days_taken,
                as_of,
            )
            movements = build_vacation_ledger(
                employee,
                benefit_rows,
                vacation_rows,
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
                "movements": movements,
            }
        )
    return balances, errors
