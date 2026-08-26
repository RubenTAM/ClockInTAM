from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta

import pymssql
import requests
from dotenv import load_dotenv

from nomina.contpaqi_vacations import read_vacation_balances


TEST_DATABASE = "ctTecno_DEV"


class VacationConnectorError(RuntimeError):
    pass


def required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise VacationConnectorError(f"Falta configurar {name}.")
    return value


def require_test_database(database: str) -> None:
    if database.strip().casefold() != TEST_DATABASE.casefold():
        raise VacationConnectorError(
            f"Bloqueo de seguridad: esta versión solo acepta {TEST_DATABASE}."
        )


def requested_days_and_rest_positions(
    start_date: date, end_date: date
) -> tuple[int, str]:
    if end_date < start_date:
        raise VacationConnectorError("El periodo solicitado no es válido.")
    total_days = (end_date - start_date).days + 1
    sunday_positions = [
        str(offset + 1)
        for offset in range(total_days)
        if (start_date + timedelta(days=offset)).weekday() == 6
    ]
    return total_days - len(sunday_positions), ",".join(sunday_positions)


@dataclass(frozen=True)
class Settings:
    sql_server: str
    sql_port: int
    sql_database: str
    sql_username: str
    sql_password: str
    tiempo_base_url: str
    tiempo_token: str

    @classmethod
    def from_environment(cls) -> "Settings":
        database = required_environment("CONTPAQ_SQL_DATABASE")
        require_test_database(database)
        return cls(
            sql_server=required_environment("CONTPAQ_SQL_SERVER"),
            sql_port=int(os.getenv("CONTPAQ_SQL_PORT", "1433")),
            sql_database=database,
            sql_username=required_environment("CONTPAQ_SQL_USER"),
            sql_password=required_environment("CONTPAQ_SQL_PASSWORD"),
            tiempo_base_url=required_environment("TIEMPO_BASE_URL").rstrip("/"),
            tiempo_token=required_environment("TIEMPO_SYNC_TOKEN"),
        )


class TiempoClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": "Tiempo-CONTPAQi-Connector/1.0",
            }
        )

    def claim(self) -> dict | None:
        response = self.session.post(
            f"{self.base_url}/api/integraciones/contpaqi/solicitudes/tomar",
            timeout=30,
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    def post_result(
        self,
        request_id: int,
        *,
        status: str,
        record_id: int | None = None,
        error: str = "",
    ) -> None:
        response = self.session.post(
            f"{self.base_url}/api/integraciones/contpaqi/solicitudes/{request_id}/resultado",
            json={"status": status, "recordId": record_id, "error": error},
            timeout=30,
        )
        response.raise_for_status()

    def sync_balance(self, balance: dict, as_of: date) -> None:
        response = self.session.post(
            f"{self.base_url}/api/integraciones/contpaqi/vacaciones",
            json={
                "source": f"CONTPAQi Nóminas · {TEST_DATABASE}",
                "asOf": as_of.isoformat(),
                "balances": [balance],
            },
            timeout=60,
        )
        response.raise_for_status()


def _employee_balance(settings: Settings, employee_code: str, as_of: date) -> dict:
    balances, errors = read_vacation_balances(
        server=settings.sql_server,
        port=settings.sql_port,
        database=settings.sql_database,
        username=settings.sql_username,
        password=settings.sql_password,
        as_of=as_of,
    )
    normalized_code = employee_code.lstrip("0") or "0"
    matches = [
        row for row in balances
        if (str(row["employeeCode"]).strip().lstrip("0") or "0")
        == normalized_code
    ]
    if len(matches) != 1:
        employee_error = next(
            (
                row["error"] for row in errors
                if (str(row["employeeCode"]).strip().lstrip("0") or "0")
                == normalized_code
            ),
            "",
        )
        raise VacationConnectorError(
            employee_error
            or "El código de trabajador no tiene una coincidencia única en CONTPAQi."
        )
    return matches[0]


def apply_request(settings: Settings, item: dict) -> int:
    require_test_database(settings.sql_database)
    try:
        employee_code = str(item["employeeCode"]).strip()
        start_date = date.fromisoformat(str(item["startDate"]))
        end_date = date.fromisoformat(str(item["endDate"]))
        requested_days = int(item["requestedDays"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VacationConnectorError("La solicitud recibida está incompleta.") from exc
    if not employee_code:
        raise VacationConnectorError("El trabajador no tiene número de empleado.")
    calculated_days, rest_positions = requested_days_and_rest_positions(
        start_date, end_date
    )
    if calculated_days != requested_days:
        raise VacationConnectorError(
            "Los días solicitados no coinciden con el periodo y sus domingos."
        )
    try:
        connection = pymssql.connect(
            server=settings.sql_server, port=settings.sql_port,
            user=settings.sql_username, password=settings.sql_password,
            database=settings.sql_database, login_timeout=8,
            timeout=30, autocommit=False,
        )
    except Exception as exc:
        raise VacationConnectorError(
            "No fue posible conectarse a la base de pruebas de CONTPAQi."
        ) from exc
    try:
        cursor = connection.cursor(as_dict=True)
        cursor.execute("SET LOCK_TIMEOUT 5000")
        cursor.execute(
            """
            SELECT IdEmpleado FROM NOM10001 WITH (UPDLOCK, HOLDLOCK)
            WHERE LTRIM(RTRIM(CodigoEmpleado)) = %s
               OR TRY_CONVERT(INT, CodigoEmpleado) = TRY_CONVERT(INT, %s)
            """,
            (employee_code, employee_code),
        )
        employees = cursor.fetchall()
        if len(employees) != 1:
            raise VacationConnectorError(
                "El código de trabajador no tiene una coincidencia única en CONTPAQi."
            )
        employee_id = int(employees[0]["IdEmpleado"])
        cursor.execute(
            """
            SELECT IdTControlVacaciones FROM NOM10014 WITH (UPDLOCK, HOLDLOCK)
            WHERE IdEmpleado = %s AND FechaInicio = %s AND FechaFin = %s
              AND DiasVacaciones = %s
            """,
            (employee_id, start_date, end_date, requested_days),
        )
        exact = cursor.fetchone()
        if exact is not None:
            connection.rollback()
            return int(exact["IdTControlVacaciones"])
        current_balance = _employee_balance(
            settings, employee_code, date.today()
        )
        if float(current_balance["availableDays"]) < requested_days:
            raise VacationConnectorError(
                "El saldo actual de CONTPAQi ya no alcanza para esta solicitud."
            )
        cursor.execute(
            """
            SELECT TOP 1 IdTControlVacaciones
            FROM NOM10014 WITH (UPDLOCK, HOLDLOCK)
            WHERE IdEmpleado = %s
              AND NOT (FechaFin < %s OR FechaInicio > %s)
            """,
            (employee_id, start_date, end_date),
        )
        if cursor.fetchone() is not None:
            raise VacationConnectorError(
                "Ya existe otro periodo de vacaciones traslapado en CONTPAQi."
            )
        cursor.execute(
            """
            INSERT INTO NOM10014 (
                IdEmpleado, Ejercicio, DiasVacaciones, DiasPrimaVacacional,
                FechaInicio, FechaFin, DiasDescanso, [TimeStamp], FechaPago
            ) OUTPUT INSERTED.IdTControlVacaciones
            VALUES (%s, %s, %s, %s, %s, %s, %s, GETDATE(), %s)
            """,
            (employee_id, start_date.year, requested_days, 0.0,
             start_date, end_date, rest_positions, start_date),
        )
        inserted = cursor.fetchone()
        if inserted is None:
            raise VacationConnectorError("CONTPAQi no devolvió el folio insertado.")
        record_id = int(inserted["IdTControlVacaciones"])
        connection.commit()
        return record_id
    except VacationConnectorError:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        raise VacationConnectorError(
            "CONTPAQi rechazó la escritura de la solicitud."
        ) from exc
    finally:
        connection.close()


def process_one(settings: Settings, client: TiempoClient) -> bool:
    item = client.claim()
    if item is None:
        return False
    request_id = int(item["requestId"])
    try:
        record_id = apply_request(settings, item)
        as_of = date.today()
        balance = _employee_balance(settings, str(item["employeeCode"]), as_of)
        client.sync_balance(balance, as_of)
        client.post_result(request_id, status="applied", record_id=record_id)
        print(f"Solicitud {request_id} aplicada. Folio {record_id}.")
    except Exception as exc:
        message = (
            str(exc) if isinstance(exc, VacationConnectorError)
            else "El conector encontró un error inesperado."
        )
        try:
            client.post_result(request_id, status="failed", error=message)
        except requests.RequestException:
            pass
        print(f"Solicitud {request_id} detenida: {message}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aplica en CONTPAQi las vacaciones aprobadas en Tiempo."
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("El intervalo mínimo es de 5 segundos.")
    load_dotenv()
    settings = Settings.from_environment()
    client = TiempoClient(settings.tiempo_base_url, settings.tiempo_token)
    while True:
        process_one(settings, client)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
