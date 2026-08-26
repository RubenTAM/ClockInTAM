from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def provision_worker_accounts(
    database_path: str,
    *,
    group: str,
    temporary_password: str,
) -> dict:
    if len(temporary_password) < 9:
        raise ValueError("La contraseña temporal debe tener al menos 9 caracteres.")
    path = Path(database_path)
    if not path.is_file():
        raise ValueError("No se encontró la base de datos del dashboard.")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        employees = connection.execute(
            """
            SELECT employee_name_key, employee_name, employee_code
            FROM employees
            WHERE LOWER(TRIM(area)) = LOWER(TRIM(?))
            ORDER BY CAST(employee_code AS INTEGER), employee_name
            """,
            (group,),
        ).fetchall()
        if not employees:
            raise ValueError("No hay trabajadores registrados en ese grupo.")
        password_hash = generate_password_hash(
            temporary_password, method="pbkdf2:sha256"
        )
        created = []
        reset = []
        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        for employee in employees:
            existing = connection.execute(
                """
                SELECT id, username FROM users
                WHERE employee_name_key = ? AND access_role = 'worker'
                """,
                (employee["employee_name_key"],),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, must_change_password = 1, active = 1
                    WHERE id = ?
                    """,
                    (password_hash, existing["id"]),
                )
                reset.append(existing["username"])
                continue
            username = str(employee["employee_code"] or "").strip()
            if not username:
                raise ValueError(
                    f"{employee['employee_name']} no tiene número de empleado."
                )
            collision = connection.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    f"El usuario {username} ya está ocupado por otra cuenta."
                )
            connection.execute(
                """
                INSERT INTO users (
                    username, display_name, password_hash, access_role,
                    employee_name_key, must_change_password, active,
                    created_at, supervised_area
                ) VALUES (?, ?, ?, 'worker', ?, 1, 1, ?, '')
                """,
                (
                    username,
                    employee["employee_name"],
                    password_hash,
                    employee["employee_name_key"],
                    now,
                ),
            )
            created.append(username)
        connection.commit()
        return {
            "employees": len(employees),
            "created": created,
            "reset": reset,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crea las cuentas de trabajadores de un grupo."
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--group", default="TecnoAll - Ingenieria")
    parser.add_argument("--temporary-password", required=True)
    args = parser.parse_args()
    result = provision_worker_accounts(
        args.database,
        group=args.group,
        temporary_password=args.temporary_password,
    )
    print(f"Trabajadores encontrados: {result['employees']}")
    print(f"Cuentas creadas: {len(result['created'])}")
    print(f"Cuentas existentes reiniciadas: {len(result['reset'])}")
    if result["created"]:
        print("Usuarios nuevos: " + ", ".join(result["created"]))
    if result["reset"]:
        print("Usuarios conservados: " + ", ".join(result["reset"]))


if __name__ == "__main__":
    main()
