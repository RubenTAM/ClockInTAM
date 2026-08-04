from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overtime_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_name TEXT NOT NULL,
    employee_name_key TEXT NOT NULL,
    work_date TEXT NOT NULL,
    allowed_start TEXT NOT NULL,
    allowed_end TEXT NOT NULL,
    approved_minutes INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS employees (
    employee_name_key TEXT PRIMARY KEY,
    employee_name TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_authorizations_date
ON overtime_authorizations(work_date);

CREATE INDEX IF NOT EXISTS idx_authorizations_employee
ON overtime_authorizations(employee_name_key);

CREATE INDEX IF NOT EXISTS idx_employees_area
ON employees(area);
"""


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        database_path = Path(current_app.config["DATABASE_PATH"])
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        g.db = connection
    return g.db


def close_db(_error=None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db() -> None:
    connection = get_db()
    connection.executescript(SCHEMA)
    migrate_multiple_authorizations(connection)
    migrate_approved_minutes(connection)
    connection.commit()


def migrate_approved_minutes(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info('overtime_authorizations')"
        ).fetchall()
    }
    if "approved_minutes" not in columns:
        connection.execute(
            """
            ALTER TABLE overtime_authorizations
            ADD COLUMN approved_minutes INTEGER
            """
        )


def migrate_multiple_authorizations(
    connection: sqlite3.Connection,
) -> None:
    unique_employee_date = False
    for index in connection.execute(
        "PRAGMA index_list('overtime_authorizations')"
    ).fetchall():
        if not index["unique"]:
            continue
        columns = [
            row["name"]
            for row in connection.execute(
                f"PRAGMA index_info('{index['name']}')"
            ).fetchall()
        ]
        if columns == ["employee_name_key", "work_date"]:
            unique_employee_date = True
            break

    if not unique_employee_date:
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(
        """
        DROP INDEX IF EXISTS idx_authorizations_date;
        DROP INDEX IF EXISTS idx_authorizations_employee;

        ALTER TABLE overtime_authorizations
        RENAME TO overtime_authorizations_legacy;

        CREATE TABLE overtime_authorizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT NOT NULL,
            employee_name_key TEXT NOT NULL,
            work_date TEXT NOT NULL,
            allowed_start TEXT NOT NULL,
            allowed_end TEXT NOT NULL,
            approved_minutes INTEGER,
            note TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (created_by) REFERENCES users(id)
        );

        INSERT INTO overtime_authorizations (
            id, employee_name, employee_name_key, work_date,
            allowed_start, allowed_end, note, created_by,
            created_at, updated_at
        )
        SELECT
            id, employee_name, employee_name_key, work_date,
            allowed_start, allowed_end, note, created_by,
            created_at, updated_at
        FROM overtime_authorizations_legacy;

        DROP TABLE overtime_authorizations_legacy;

        CREATE INDEX idx_authorizations_date
        ON overtime_authorizations(work_date);

        CREATE INDEX idx_authorizations_employee
        ON overtime_authorizations(employee_name_key);
        """
    )
    connection.execute("PRAGMA foreign_keys = ON")


def log_action(
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    details: str = "",
) -> None:
    connection = get_db()
    connection.execute(
        """
        INSERT INTO audit_log (
            user_id, action, entity_type, entity_id, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            action,
            entity_type,
            entity_id,
            details,
            utc_now(),
        ),
    )


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
