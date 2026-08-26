from __future__ import annotations

from datetime import date, datetime

import pymssql


class ContpaqiPayrollError(RuntimeError):
    pass


def _iso_date(value) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        raise ContpaqiPayrollError("CONTPAQi devolvió una fecha inválida.")
    return value.isoformat()


def _iso_datetime(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ContpaqiPayrollError("CONTPAQi devolvió una fecha de emisión inválida.")
    return value.replace(microsecond=0).isoformat()


def _category(concept_type: str, sat_code: str, amount: float) -> str | None:
    if concept_type == "P":
        return "perception"
    if concept_type != "D":
        return None
    if amount < 0:
        return "other_payment"
    return "withholding" if sat_code == "002" else "deduction"


def read_payroll_receipts(
    *,
    server: str,
    port: int,
    database: str,
    username: str,
    password: str,
    employee_code: str,
    receipt_uuid: str | None = None,
    year: int | None = None,
    limit: int = 60,
) -> list[dict]:
    """Read stamped payroll receipts and printable concepts for one employee."""
    if not str(employee_code or "").strip():
        raise ContpaqiPayrollError("Es obligatorio indicar el número de empleado.")
    if limit < 1 or limit > 250:
        raise ContpaqiPayrollError("El límite de recibos no es válido.")
    try:
        connection = pymssql.connect(
            server=server,
            port=port,
            user=username,
            password=password,
            database=database,
            login_timeout=8,
            timeout=60,
            autocommit=False,
        )
    except Exception as exc:
        raise ContpaqiPayrollError(
            "No fue posible conectarse a CONTPAQi Nóminas."
        ) from exc

    try:
        cursor = connection.cursor(as_dict=True)
        cursor.execute("SET LOCK_TIMEOUT 5000")
        cursor.execute(
            """
            SELECT IdEmpleado, CodigoEmpleado, NombreLargo
            FROM NOM10001
            WHERE LTRIM(RTRIM(CodigoEmpleado)) = %s
            """,
            (str(employee_code).strip(),),
        )
        employees = cursor.fetchall()
        if len(employees) != 1 and str(employee_code).strip().isdigit():
            normalized = str(int(str(employee_code).strip()))
            cursor.execute(
                """
                SELECT IdEmpleado, CodigoEmpleado, NombreLargo
                FROM NOM10001
                WHERE LTRIM(RTRIM(CodigoEmpleado)) IN (%s, %s, %s)
                """,
                (normalized, normalized.zfill(3), normalized.zfill(4)),
            )
            employees = cursor.fetchall()
        if len(employees) != 1:
            raise ContpaqiPayrollError(
                "El número de empleado no tiene una coincidencia única en CONTPAQi."
            )
        employee = employees[0]
        uuid_filter = "AND UPPER(d.UUID) = %s" if receipt_uuid else ""
        year_filter = "AND p.ejercicio = %s" if year is not None else ""
        query_parameters = [limit, int(employee["IdEmpleado"])]
        if receipt_uuid:
            query_parameters.append(str(receipt_uuid).strip().upper())
        if year is not None:
            query_parameters.append(int(year))
        cursor.execute(
            f"""
            SELECT TOP %s
                d.IdDocumento, d.IdPeriodo, d.FechaPago, d.FechaEmision,
                d.NumDiasPagados, d.UUID, d.FechaInicialPago,
                d.FechaFinalPago, p.numeroperiodo,
                tp.nombretipoperiodo
            FROM NOM10043 d
            JOIN NOM10002 p ON p.idperiodo = d.IdPeriodo
            LEFT JOIN NOM10023 tp ON tp.idtipoperiodo = p.idtipoperiodo
            WHERE d.IdEmpleado = %s
              AND LTRIM(RTRIM(ISNULL(d.UUID, ''))) <> ''
              AND d.Estado = 3
              {uuid_filter}
              {year_filter}
            ORDER BY d.FechaPago DESC, d.IdDocumento DESC
            """,
            tuple(query_parameters),
        )
        documents = cursor.fetchall()
        receipts = []
        for document in documents:
            cursor.execute(
                """
                SELECT
                    c.numeroconcepto, c.descripcion, c.tipoconcepto,
                    c.ClaveAgrupadoraSAT, m.importetotal
                FROM NOM10007 m
                JOIN NOM10004 c ON c.idconcepto = m.idconcepto
                WHERE m.idempleado = %s AND m.idperiodo = %s
                  AND ABS(m.importetotal) > 0.00001
                ORDER BY c.tipoconcepto, c.numeroconcepto
                """,
                (int(employee["IdEmpleado"]), int(document["IdPeriodo"])),
            )
            movements = cursor.fetchall()
            items = []
            net_pay = None
            for movement in movements:
                concept_type = str(movement["tipoconcepto"] or "").strip().upper()
                amount = float(movement["importetotal"] or 0)
                if concept_type == "N" and int(movement["numeroconcepto"] or 0) == 0:
                    net_pay = amount
                    continue
                sat_code = str(movement["ClaveAgrupadoraSAT"] or "").strip()
                category = _category(concept_type, sat_code, amount)
                if category is None or not sat_code:
                    continue
                items.append(
                    {
                        "category": category,
                        "satCode": sat_code,
                        "conceptNumber": int(movement["numeroconcepto"]),
                        "conceptName": str(movement["descripcion"] or "").strip(),
                        "amount": round(abs(amount), 2),
                    }
                )
            gross_pay = sum(
                item["amount"] for item in items
                if item["category"] in {"perception", "other_payment"}
            )
            deductions = sum(
                item["amount"] for item in items if item["category"] == "deduction"
            )
            withholdings = sum(
                item["amount"] for item in items if item["category"] == "withholding"
            )
            calculated_net = gross_pay - deductions - withholdings
            receipts.append(
                {
                    "sourceDocumentId": int(document["IdDocumento"]),
                    "employeeCode": str(employee["CodigoEmpleado"] or "").strip(),
                    "employeeName": str(employee["NombreLargo"] or "").strip(),
                    "uuid": str(document["UUID"] or "").strip().upper(),
                    "periodId": int(document["IdPeriodo"]),
                    "periodType": str(document["nombretipoperiodo"] or "").strip(),
                    "periodNumber": int(document["numeroperiodo"]),
                    "periodStart": _iso_date(document["FechaInicialPago"]),
                    "periodEnd": _iso_date(document["FechaFinalPago"]),
                    "paymentDate": _iso_date(document["FechaPago"]),
                    "issuedAt": _iso_datetime(document["FechaEmision"]),
                    "paidDays": float(document["NumDiasPagados"] or 0),
                    "grossPay": round(gross_pay, 2),
                    "deductions": round(deductions, 2),
                    "withholdings": round(withholdings, 2),
                    "netPay": round(net_pay if net_pay is not None else calculated_net, 2),
                    "items": items,
                }
            )
        connection.rollback()
        return receipts
    except ContpaqiPayrollError:
        connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        raise ContpaqiPayrollError(
            "No fue posible consultar los recibos de nómina en CONTPAQi."
        ) from exc
    finally:
        connection.close()
