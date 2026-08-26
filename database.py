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
    access_role TEXT NOT NULL DEFAULT 'admin',
    employee_name_key TEXT NOT NULL DEFAULT '',
    must_change_password INTEGER NOT NULL DEFAULT 0,
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
    employee_code TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    vacation_days_available REAL,
    vacation_balance_as_of TEXT,
    vacation_synced_at TEXT,
    vacation_source TEXT NOT NULL DEFAULT '',
    contpaqi_employee_id INTEGER,
    contpaqi_employee_name TEXT NOT NULL DEFAULT '',
    contpaqi_employee_status TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vacations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_name TEXT NOT NULL,
    employee_name_key TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (created_by) REFERENCES users(id),
    CHECK (end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS vacation_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_name TEXT NOT NULL,
    employee_name_key TEXT NOT NULL,
    supervisor_id INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    requested_days INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    responded_at TEXT,
    decided_by INTEGER,
    worker_read_at TEXT,
    worker_deleted_at TEXT,
    sent_to_all INTEGER NOT NULL DEFAULT 0,
    contpaqi_status TEXT NOT NULL DEFAULT 'not_queued',
    contpaqi_attempts INTEGER NOT NULL DEFAULT 0,
    contpaqi_error TEXT NOT NULL DEFAULT '',
    contpaqi_applied_at TEXT,
    contpaqi_record_id INTEGER,
    contpaqi_locked_at TEXT,
    contpaqi_updated_at TEXT,
    FOREIGN KEY (supervisor_id) REFERENCES users(id),
    FOREIGN KEY (decided_by) REFERENCES users(id),
    CHECK (end_date >= start_date),
    CHECK (requested_days > 0),
    CHECK (status IN ('pending', 'approved', 'rejected')),
    CHECK (contpaqi_status IN (
        'not_queued', 'pending', 'processing', 'applied', 'failed'
    ))
);

CREATE TABLE IF NOT EXISTS vacation_request_recipients (
    request_id INTEGER NOT NULL,
    supervisor_id INTEGER NOT NULL,
    deleted_at TEXT,
    PRIMARY KEY (request_id, supervisor_id),
    FOREIGN KEY (request_id) REFERENCES vacation_requests(id),
    FOREIGN KEY (supervisor_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS employee_vacation_movements (
    employee_name_key TEXT NOT NULL,
    source_movement_key TEXT NOT NULL,
    concept TEXT NOT NULL,
    registered_date TEXT,
    start_date TEXT,
    end_date TEXT,
    days_taken REAL NOT NULL DEFAULT 0,
    days_entitled REAL NOT NULL DEFAULT 0,
    balance REAL NOT NULL,
    balance_as_of TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (employee_name_key, source_movement_key),
    FOREIGN KEY (employee_name_key) REFERENCES employees(employee_name_key)
);

CREATE TABLE IF NOT EXISTS payroll_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER NOT NULL,
    employee_name_key TEXT NOT NULL,
    employee_code TEXT NOT NULL,
    uuid TEXT NOT NULL UNIQUE COLLATE NOCASE,
    period_id INTEGER NOT NULL,
    period_type TEXT NOT NULL DEFAULT '',
    period_number INTEGER,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    payment_date TEXT NOT NULL,
    issued_at TEXT,
    paid_days REAL NOT NULL DEFAULT 0,
    gross_pay REAL NOT NULL DEFAULT 0,
    deductions REAL NOT NULL DEFAULT 0,
    withholdings REAL NOT NULL DEFAULT 0,
    net_pay REAL NOT NULL DEFAULT 0,
    pdf_filename TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    synced_at TEXT NOT NULL,
    FOREIGN KEY (employee_name_key) REFERENCES employees(employee_name_key)
);

CREATE TABLE IF NOT EXISTS payroll_receipt_items (
    receipt_id INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    category TEXT NOT NULL,
    sat_code TEXT NOT NULL DEFAULT '',
    concept_number INTEGER NOT NULL,
    concept_name TEXT NOT NULL,
    amount REAL NOT NULL,
    PRIMARY KEY (receipt_id, line_number),
    FOREIGN KEY (receipt_id) REFERENCES payroll_receipts(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attendance_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_name_key TEXT NOT NULL,
    work_date TEXT NOT NULL,
    granted_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (granted_by) REFERENCES users(id),
    UNIQUE(employee_name_key, work_date)
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

CREATE INDEX IF NOT EXISTS idx_vacations_dates
ON vacations(start_date, end_date);

CREATE INDEX IF NOT EXISTS idx_vacations_employee
ON vacations(employee_name_key);

CREATE INDEX IF NOT EXISTS idx_vacation_requests_supervisor
ON vacation_requests(supervisor_id, status);

CREATE INDEX IF NOT EXISTS idx_vacation_requests_employee
ON vacation_requests(employee_name_key, requested_at);

CREATE INDEX IF NOT EXISTS idx_vacation_request_recipients_supervisor
ON vacation_request_recipients(supervisor_id, request_id);

CREATE INDEX IF NOT EXISTS idx_employee_vacation_movements_employee
ON employee_vacation_movements(employee_name_key, registered_date);

CREATE INDEX IF NOT EXISTS idx_payroll_receipts_employee
ON payroll_receipts(employee_name_key, payment_date DESC);

CREATE INDEX IF NOT EXISTS idx_attendance_permissions_date
ON attendance_permissions(work_date);
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
    migrate_user_supervised_area(connection)
    migrate_user_worker_access(connection)
    migrate_forced_password_change(connection)
    migrate_multiple_authorizations(connection)
    migrate_approved_minutes(connection)
    migrate_employee_code(connection)
    migrate_employee_vacation_balance(connection)
    migrate_vacation_request_recipients(connection)
    migrate_vacation_request_integration(connection)
    connection.commit()


def migrate_user_supervised_area(connection: sqlite3.Connection) -> None:
    """Add supervisor scope and seed the two known Engineering leads."""
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info('users')").fetchall()
    }
    if "supervised_area" in columns:
        return

    connection.execute(
        """
        ALTER TABLE users
        ADD COLUMN supervised_area TEXT NOT NULL DEFAULT ''
        """
    )
    connection.execute(
        """
        UPDATE users
        SET supervised_area = 'TecnoAll - Ingenieria'
        WHERE (
            (LOWER(display_name) LIKE '%ruben%'
             OR LOWER(display_name) LIKE '%rubén%')
            AND LOWER(display_name) LIKE '%lizarraga%'
        ) OR (
            (LOWER(display_name) LIKE '%jose%'
             OR LOWER(display_name) LIKE '%josé%')
            AND LOWER(display_name) LIKE '%valdez%'
        )
        """
    )


def migrate_user_worker_access(connection: sqlite3.Connection) -> None:
    """Add an explicit worker role and its linked employee profile."""
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info('users')").fetchall()
    }
    if "access_role" not in columns:
        connection.execute(
            """
            ALTER TABLE users
            ADD COLUMN access_role TEXT NOT NULL DEFAULT 'admin'
            """
        )
    if "employee_name_key" not in columns:
        connection.execute(
            """
            ALTER TABLE users
            ADD COLUMN employee_name_key TEXT NOT NULL DEFAULT ''
            """
        )


def migrate_forced_password_change(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info('users')").fetchall()
    }
    if "must_change_password" not in columns:
        connection.execute(
            """
            ALTER TABLE users
            ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0
            """
        )


def migrate_employee_code(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info('employees')"
        ).fetchall()
    }
    if "employee_code" not in columns:
        connection.execute(
            """
            ALTER TABLE employees
            ADD COLUMN employee_code TEXT NOT NULL DEFAULT ''
            """
        )


def migrate_employee_vacation_balance(
    connection: sqlite3.Connection,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info('employees')"
        ).fetchall()
    }
    additions = {
        "vacation_days_available": "REAL",
        "vacation_balance_as_of": "TEXT",
        "vacation_synced_at": "TEXT",
        "vacation_source": "TEXT NOT NULL DEFAULT ''",
        "contpaqi_employee_id": "INTEGER",
        "contpaqi_employee_name": "TEXT NOT NULL DEFAULT ''",
        "contpaqi_employee_status": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE employees ADD COLUMN {name} {definition}"
            )


def migrate_vacation_request_recipients(
    connection: sqlite3.Connection,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info('vacation_requests')"
        ).fetchall()
    }
    additions = {
        "worker_deleted_at": "TEXT",
        "sent_to_all": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE vacation_requests ADD COLUMN {name} {definition}"
            )
    connection.execute(
        """
        INSERT OR IGNORE INTO vacation_request_recipients (
            request_id, supervisor_id
        )
        SELECT id, supervisor_id FROM vacation_requests
        """
    )


def migrate_vacation_request_integration(
    connection: sqlite3.Connection,
) -> None:
    """Add the durable outbox used by the local CONTPAQi connector."""
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info('vacation_requests')"
        ).fetchall()
    }
    additions = {
        "contpaqi_status": "TEXT NOT NULL DEFAULT 'not_queued'",
        "contpaqi_attempts": "INTEGER NOT NULL DEFAULT 0",
        "contpaqi_error": "TEXT NOT NULL DEFAULT ''",
        "contpaqi_applied_at": "TEXT",
        "contpaqi_record_id": "INTEGER",
        "contpaqi_locked_at": "TEXT",
        "contpaqi_updated_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE vacation_requests ADD COLUMN {name} {definition}"
            )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vacation_requests_contpaqi
        ON vacation_requests(contpaqi_status, id)
        """
    )


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
