from __future__ import annotations

import argparse
import os
from datetime import date

import requests
from dotenv import load_dotenv

from nomina.contpaqi_vacations import read_vacation_balances


def required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta configurar {name}.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincroniza saldos de vacaciones de CONTPAQi con Tiempo."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Consulta CONTPAQi sin enviar información a Tiempo.",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        help="Fecha de corte en formato AAAA-MM-DD.",
    )
    args = parser.parse_args()
    load_dotenv()

    balances, errors = read_vacation_balances(
        server=required_environment("CONTPAQ_SQL_SERVER"),
        port=int(os.getenv("CONTPAQ_SQL_PORT", "1433")),
        database=required_environment("CONTPAQ_SQL_DATABASE"),
        username=required_environment("CONTPAQ_SQL_USER"),
        password=required_environment("CONTPAQ_SQL_PASSWORD"),
        as_of=args.as_of,
    )
    print(f"Saldos calculados: {len(balances)}")
    print(f"Empleados con error: {len(errors)}")
    if args.dry_run:
        for row in balances:
            print(f"{row['employeeCode']}: {row['availableDays']}")
        return

    base_url = required_environment("TIEMPO_BASE_URL").rstrip("/")
    token = required_environment("TIEMPO_SYNC_TOKEN")
    response = requests.post(
        f"{base_url}/api/integraciones/contpaqi/vacaciones",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source": "CONTPAQi Nóminas",
            "asOf": args.as_of.isoformat(),
            "balances": balances,
        },
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    print(f"Actualizados en Tiempo: {result['updated']}")
    print(f"Sin correspondencia: {result['unmatched']}")


if __name__ == "__main__":
    main()
