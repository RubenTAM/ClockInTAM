from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from nomina.contpaqi_payroll import (
    ContpaqiPayrollError,
    read_payroll_receipts,
)


TEST_DATABASE = "ctTecno_DEV"


def required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ContpaqiPayrollError(f"Falta configurar {name}.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincroniza recibos timbrados de un trabajador con Tiempo."
    )
    parser.add_argument("--employee-code", required=True)
    parser.add_argument("--uuid")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    if args.pdf and not args.uuid:
        parser.error("--pdf requiere indicar también --uuid.")
    load_dotenv(args.env_file)
    database = required_environment("CONTPAQ_SQL_DATABASE")
    if database.casefold() != TEST_DATABASE.casefold():
        raise ContpaqiPayrollError(
            f"Bloqueo de seguridad: esta versión solo acepta {TEST_DATABASE}."
        )
    receipts = read_payroll_receipts(
        server=required_environment("CONTPAQ_SQL_SERVER"),
        port=int(os.getenv("CONTPAQ_SQL_PORT", "1433")),
        database=database,
        username=required_environment("CONTPAQ_SQL_USER"),
        password=required_environment("CONTPAQ_SQL_PASSWORD"),
        employee_code=args.employee_code,
        receipt_uuid=args.uuid,
        limit=args.limit,
    )
    if args.pdf:
        pdf_bytes = args.pdf.read_bytes()
        if len(receipts) != 1 or not pdf_bytes.startswith(b"%PDF-"):
            raise ContpaqiPayrollError(
                "El PDF solo puede adjuntarse a un recibo válido y único."
            )
        receipts[0]["pdfBase64"] = base64.b64encode(pdf_bytes).decode("ascii")
    if args.dry_run:
        for receipt in receipts:
            print(
                receipt["employeeCode"], receipt["periodNumber"],
                receipt["paymentDate"], receipt["uuid"], receipt["netPay"],
            )
        return
    base_url = required_environment("TIEMPO_BASE_URL").rstrip("/")
    token = required_environment("TIEMPO_SYNC_TOKEN")
    response = requests.post(
        f"{base_url}/api/integraciones/contpaqi/recibos-nomina",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source": f"CONTPAQi Nóminas · {database}",
            "receipts": receipts,
        },
        timeout=120,
    )
    response.raise_for_status()
    print(f"Recibos sincronizados: {response.json()['updated']}")


if __name__ == "__main__":
    main()
