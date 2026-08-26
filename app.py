from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_from_directory,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

import database
from database import get_db, log_action, utc_now
from checador.excel_reports import (
    build_accountant_rows,
    build_expediente_rows,
    create_accountant_workbook,
    create_expedientes_workbook,
    round_overtime_code_hours,
)
from checador.hikconnect import HikConnectClient, HikConnectError
from checador.reporting import (
    build_daily_attendance,
    build_weekly_report,
    format_minutes,
    normalize_name,
)


load_dotenv()

DAY_NAMES = (
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
)
MONTH_NAMES = (
    "",
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

PHOTO_DIRECTORY = Path(__file__).resolve().parent / "fotos"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_NAME_ALIASES = {
    "dalia": "dalila",
    "feliz": "felix",
    "plascencia": "plasencia",
    "reyez": "reyes",
    "susniaga": "suniaga",
}
PHOTO_NAME_IGNORED = {"fotor", "practicante"}
EMPLOYEE_DIRECTORY = (
    "Ruben Humberto Lizarraga Reyes",
)
KNOWN_EMPLOYEE_CODES = (
    ("001", "LIZARRAGA CARRAZCO VLADIMIR"),
    ("003", "LIZARRAGA CARRAZCO PETRA NOEMI"),
    ("009", "VALDEZ GUZMAN JOSE"),
    ("012", "MARTINEZ MARTINEZ ERIKA IRIDEA"),
    ("013", "GUZMAN GAMEZ LUIS ANGEL"),
    ("014", "MARTINEZ MARTINEZ JESUS MANUEL"),
    ("016", "HERRERA CELIS JESUS ALFONSO"),
    ("017", "LIZARRAGA REYES RUBEN HUMBERTO"),
    ("018", "CAMACHO ALONSO HECTOR ANTONIO"),
    ("020", "HERNANDEZ VELAZQUEZ MARIO ANGEL"),
    ("024", "LOPEZ FELIX LUIS GUSTAVO"),
    ("025", "CASTELLANOS LANDGRAVE JESSICA ALEJANDRA"),
    ("027", "LIZARRAGA CARRAZCO RUBEN ILLIA"),
    ("036", "TLATEMPA PAISANO KAREN JAZMIN"),
    ("037", "GUTIERREZ ESCARREGA MARIA FERNANDA"),
    ("051", "VALENZUELA FLORES LUIS"),
    ("053", "ZUÑIGA ESTRADA JESUS MANUEL"),
    ("061", "PALMA SERRATO JOSEFINA"),
    ("063", "RANGEL PULIDO JORGE"),
    ("069", "CASTILLO AGUILAR MARTIN DAVID"),
    ("071", "LOPEZ JAUREGUI GERARDO"),
    ("073", "VARGAS SANZON LUIS DE JESUS"),
    ("077", "SANCHEZ PEREZ RICARDO JOEL"),
    ("080", "SANCHEZ REYNA EDUARDO"),
    ("082", "TISCAREÑO CINTORA SEBASTIAN"),
    ("086", "MEJIA MORALES GERONIMO IVAN"),
    ("089", "LOPEZ ZAMORA MARIA FERNANDA"),
    ("090", "RODRIGUEZ GUZMAN ARABELLA"),
    ("094", "RODRIGUEZ PLASENCIA DIANA JAZMIN"),
    ("095", "REYES DIAZ HEIDI JOHANA"),
    ("096", "CEBALLOS RESENDIZ KURT SAMUEL"),
    ("098", "TREJO HIGUERA DADINIRT GUADALUPE"),
    ("100", "CORTES PEREZ IRAIDA DALILA"),
    ("101", "CHAVEZ CABRERA JUAN"),
    ("102", "BAEZ MEZA JOSE LUIS"),
    ("103", "LIZARRAGA FLORES ANDRES VLADIMIR"),
    ("104", "CERVANTES PEREDIA RANDY EMMANUEL"),
    ("105", "MARTINEZ LIZARRAGA LUIS NATIVIDAD"),
    ("106", "FLORES LOPEZ EMILIO ESAU"),
    ("107", "GUZMAN GAMEZ PEDRO ANTONIO"),
    ("108", "SUNIAGA MEJIAS MARCOS ELIAS"),
)


def employee_identity_key(value: str) -> tuple[str, ...]:
    """Match employee names even when surnames are listed first."""
    return tuple(
        sorted(
            PHOTO_NAME_ALIASES.get(token, token)
            for token in normalize_name(value).split()
        )
    )


EMPLOYEE_CODES_BY_NAME = {
    employee_identity_key(name): code
    for code, name in KNOWN_EMPLOYEE_CODES
}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def known_employee_code(employee_name: str) -> str:
    return EMPLOYEE_CODES_BY_NAME.get(
        employee_identity_key(employee_name),
        "",
    )


def valid_email(value: str) -> bool:
    return len(value) <= 254 and EMAIL_PATTERN.fullmatch(value) is not None


def integration_authenticated() -> bool:
    configured_token = str(
        current_app.config.get("CONTPAQ_SYNC_TOKEN") or ""
    )
    authorization = request.headers.get("Authorization", "")
    provided_token = (
        authorization[7:].strip()
        if authorization.startswith("Bearer ")
        else ""
    )
    return bool(
        configured_token
        and provided_token
        and secrets.compare_digest(provided_token, configured_token)
    )


def photo_name_key(value: str) -> tuple[str, ...]:
    """Return an order-independent worker name key from a photo filename."""
    normalized = normalize_name(Path(value).stem)
    normalized = normalized.split("-fotor", 1)[0]
    tokens = []
    for token in normalized.split():
        if token.isdigit() or token in PHOTO_NAME_IGNORED:
            continue
        tokens.append(PHOTO_NAME_ALIASES.get(token, token))
    return tuple(sorted(tokens))


def employee_photo_filename(employee_name: str) -> str | None:
    """Match a worker with a local photo using the name in its filename."""
    if not PHOTO_DIRECTORY.is_dir():
        return None
    employee_key = photo_name_key(employee_name)
    for photo_path in sorted(PHOTO_DIRECTORY.iterdir()):
        if (
            photo_path.is_file()
            and photo_path.suffix.casefold() in PHOTO_EXTENSIONS
            and photo_name_key(photo_path.name) == employee_key
        ):
            return photo_path.name
    return None


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        APP_ENV=os.getenv("APP_ENV", "development"),
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-only-change-me"),
        DATABASE_PATH=os.getenv(
            "DATABASE_PATH",
            str(Path(app.instance_path) / "checador.sqlite3"),
        ),
        HIK_API_KEY=os.getenv("HIK_API_KEY", ""),
        HIK_API_SECRET=os.getenv("HIK_API_SECRET", ""),
        HIK_BASE_URL=os.getenv(
            "HIK_BASE_URL",
            "https://ius.hikcentralconnect.com",
        ),
        APP_TIMEZONE=os.getenv("APP_TIMEZONE", "America/Tijuana"),
        CONTPAQ_SYNC_TOKEN=os.getenv("CONTPAQ_SYNC_TOKEN", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv(
            "SESSION_COOKIE_SECURE", "0"
        ).lower()
        in {"1", "true", "yes"},
        PERMANENT_SESSION_LIFETIME=timedelta(hours=10),
    )
    if test_config:
        app.config.update(test_config)

    static_directory = Path(app.static_folder)
    app.config["STATIC_VERSION"] = str(
        max(
            (static_directory / filename).stat().st_mtime_ns
            for filename in ("app.css", "app.js")
        )
    )

    if (
        app.config["APP_ENV"] == "production"
        and app.config["SECRET_KEY"] == "dev-only-change-me"
    ):
        raise RuntimeError(
            "SECRET_KEY debe configurarse antes de iniciar en producción."
        )

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
    )
    database.init_app(app)
    with app.app_context():
        save_employees(
            [
                {
                    "employee_name": employee_name,
                    "employee_name_key": normalize_name(employee_name),
                }
                for employee_name in EMPLOYEE_DIRECTORY
            ]
        )

    app.extensions["hik_client"] = HikConnectClient(
        app.config["HIK_API_KEY"],
        app.config["HIK_API_SECRET"],
        app.config["HIK_BASE_URL"],
        app.config["APP_TIMEZONE"],
    )
    app.extensions["attendance_cache"] = {}

    register_filters(app)
    register_context(app)
    register_routes(app)
    return app


def register_filters(app: Flask) -> None:
    @app.template_filter("date_es")
    def date_es(value) -> str:
        if isinstance(value, str):
            value = date.fromisoformat(value)
        return (
            f"{DAY_NAMES[value.weekday()]} {value.day} "
            f"de {MONTH_NAMES[value.month]}"
        )

    @app.template_filter("minutes")
    def minutes_filter(value) -> str:
        return format_minutes(int(value or 0))

    @app.template_filter("clock")
    def clock_filter(value) -> str:
        return value.strftime("%H:%M") if value else "—"

    @app.template_filter("weekday_short")
    def weekday_short_filter(value) -> str:
        if isinstance(value, str):
            value = date.fromisoformat(value)
        return DAY_NAMES[value.weekday()][:3]

    @app.template_filter("date_short")
    def date_short_filter(value) -> str:
        if not value:
            return "—"
        if isinstance(value, str):
            value = date.fromisoformat(value)
        return value.strftime("%d/%m/%Y")

    @app.template_filter("vacation_days")
    def vacation_days_filter(value) -> str:
        if value is None:
            return "—"
        return f"{float(value):.2f}".rstrip("0").rstrip(".")

    @app.template_filter("datetime_es")
    def datetime_es_filter(value: str | None) -> str:
        if not value:
            return "—"
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo(app.config["APP_TIMEZONE"]))
        return (
            f"{parsed.day} de {MONTH_NAMES[parsed.month]} de {parsed.year}"
            f" · {parsed:%H:%M}"
        )


def register_context(app: Flask) -> None:
    @app.before_request
    def load_current_user() -> None:
        g.user = None
        user_id = session.get("user_id")
        if user_id:
            g.user = get_db().execute(
                "SELECT * FROM users WHERE id = ? AND active = 1",
                (user_id,),
            ).fetchone()
            if g.user is None:
                session.clear()
            elif g.user["access_role"] == "worker":
                allowed_endpoints = {
                    "home",
                    "account",
                    "logout",
                    "employee_photo",
                    "request_vacation",
                    "mailbox",
                    "delete_vacation_request",
                    "clear_mailbox",
                    "static",
                }
                if request.endpoint not in allowed_endpoints:
                    abort(403)
                if (
                    g.user["must_change_password"]
                    and request.endpoint not in {"account", "logout", "static"}
                ):
                    return redirect(url_for("account"))

    @app.context_processor
    def shared_values() -> dict:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return {
            "csrf_token": token,
            "today": local_today(),
            "static_version": current_app.config["STATIC_VERSION"],
            "mailbox_badge_count": mailbox_badge_count(),
        }


def login_required(view):
    @wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(**kwargs)

    return wrapped


def validate_csrf() -> None:
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not sent or not expected or not secrets.compare_digest(sent, expected):
        abort(400, "La sesión del formulario venció. Intenta nuevamente.")


def user_count() -> int:
    row = get_db().execute("SELECT COUNT(*) AS total FROM users").fetchone()
    return int(row["total"])


def parse_iso_date(value: str, field_name: str = "fecha") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"La {field_name} no es válida.") from exc


def week_start_for(value: date) -> date:
    days_since_thursday = (value.weekday() - 3) % 7
    return value - timedelta(days=days_since_thursday)


def authorized_window_minutes(start_value: str, end_value: str) -> int:
    try:
        start = datetime.strptime(start_value, "%H:%M")
        end = datetime.strptime(end_value, "%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("El horario autorizado no es válido.") from exc
    if end == start:
        raise ValueError("La hora inicial y final deben ser diferentes.")
    if end < start:
        end += timedelta(days=1)
    return int((end - start).total_seconds() // 60)


def local_now() -> datetime:
    return datetime.now(ZoneInfo(current_app.config["APP_TIMEZONE"]))


def local_today() -> date:
    return local_now().date()


def cached_attendance(work_date: date, force: bool = False) -> list[dict]:
    cache: dict = current_app.extensions["attendance_cache"]
    key = work_date.isoformat()
    cached = cache.get(key)
    if cached and not force and monotonic() - cached["saved_at"] < 300:
        return cached["rows"]

    client: HikConnectClient = current_app.extensions["hik_client"]
    reports = client.attendance_for_date(work_date)
    rows = build_daily_attendance(reports)
    save_employees(rows)
    cache[key] = {"saved_at": monotonic(), "rows": rows}
    return rows


def save_employees(rows: list[dict]) -> None:
    """Persist workers discovered in an attendance response."""
    now = utc_now()
    employees = {}
    for row in rows:
        name_key = row.get("employee_name_key")
        employee_name = row.get("employee_name")
        if not name_key or not employee_name:
            continue
        employees[name_key] = {
            "name": employee_name,
            "code": known_employee_code(employee_name),
            "group": str(row.get("group_name") or "").strip(),
        }
    if not employees:
        return
    connection = get_db()
    connection.executemany(
        """
        INSERT INTO employees (
            employee_name_key, employee_name, employee_code, area,
            created_at, updated_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(employee_name_key) DO UPDATE SET
            employee_name = excluded.employee_name,
            employee_code = CASE
                WHEN excluded.employee_code <> '' THEN excluded.employee_code
                ELSE employees.employee_code
            END,
            area = CASE
                WHEN excluded.area <> '' THEN excluded.area
                ELSE employees.area
            END,
            updated_at = excluded.updated_at,
            last_seen_at = excluded.last_seen_at
        """,
        [
            (
                name_key,
                employee["name"],
                employee["code"],
                employee["group"],
                now,
                now,
                now,
            )
            for name_key, employee in employees.items()
        ],
    )
    connection.commit()


def employee_directory() -> list[dict]:
    rows = get_db().execute(
        """
        SELECT employee_name, employee_name_key, employee_code, area,
               vacation_days_available, vacation_balance_as_of,
               vacation_synced_at, vacation_source
        FROM employees
        ORDER BY employee_name_key
        """
    ).fetchall()
    return [dict(row) for row in rows]


def employee_groups() -> list[str]:
    rows = get_db().execute(
        """
        SELECT DISTINCT area
        FROM employees
        WHERE TRIM(area) <> ''
        ORDER BY area COLLATE NOCASE
        """
    ).fetchall()
    return [row["area"] for row in rows]


def is_inactive_group(group_name: str) -> bool:
    return "bajas e inactivos" in normalize_name(group_name)


def default_supervised_area(display_name: str) -> str:
    """Return the initial supervisor assignment requested by the business."""
    tokens = set(normalize_name(display_name).split())
    if {"ruben", "lizarraga"} <= tokens or {"jose", "valdez"} <= tokens:
        return "TecnoAll - Ingenieria"
    return ""


def same_group(first: str, second: str) -> bool:
    return normalize_name(first) == normalize_name(second)


def requested_vacation_days(start_date: date, end_date: date) -> int:
    """Count TecnoAll workdays in a request; Sundays do not consume days."""
    return sum(
        1
        for offset in range((end_date - start_date).days + 1)
        if (start_date + timedelta(days=offset)).weekday() != 6
    )


def available_supervisors() -> list:
    return get_db().execute(
        """
        SELECT id, display_name, supervised_area
        FROM users
        WHERE active = 1 AND access_role = 'admin'
        ORDER BY id
        """
    ).fetchall()


def supervisor_for_employee(area: str):
    supervisors = available_supervisors()
    scoped = [
        supervisor
        for supervisor in supervisors
        if supervisor["supervised_area"]
        and same_group(supervisor["supervised_area"], area)
    ]
    if scoped:
        return scoped[0]
    return next(
        (
            supervisor
            for supervisor in supervisors
            if not str(supervisor["supervised_area"] or "").strip()
        ),
        None,
    )


def mailbox_badge_count() -> int:
    if g.user is None:
        return 0
    if g.user["access_role"] == "worker":
        row = get_db().execute(
            """
            SELECT COUNT(*) AS total
            FROM vacation_requests
            WHERE employee_name_key = ?
              AND status IN ('approved', 'rejected')
              AND worker_read_at IS NULL
              AND worker_deleted_at IS NULL
            """,
            (g.user["employee_name_key"],),
        ).fetchone()
    else:
        row = get_db().execute(
            """
            SELECT COUNT(*) AS total
            FROM vacation_requests vr
            JOIN vacation_request_recipients recipient
              ON recipient.request_id = vr.id
            WHERE recipient.supervisor_id = ?
              AND recipient.deleted_at IS NULL
              AND vr.status = 'pending'
            """,
            (g.user["id"],),
        ).fetchone()
    return int(row["total"])


def rounded_overtime_minutes(minutes: int) -> int:
    """Apply the company 50-minute threshold and return whole-hour minutes."""
    return round_overtime_code_hours(minutes) * 60


def attendance_incident_labels(
    work_date: date,
    report: dict | None,
    *,
    day_complete: bool = True,
) -> list[str]:
    """Return every attendance incident for a scheduled workday."""
    if work_date.weekday() == 6:
        return []

    clock_in = (report or {}).get("clock_in")
    clock_out = (report or {}).get("clock_out")
    if not day_complete and clock_in is None and clock_out is None:
        return []

    incidents = []
    if clock_in is None:
        incidents.append("No checó entrada")
    else:
        late = (
            clock_in > time(8, 10)
            if work_date.weekday() < 5
            else clock_in >= time(8, 50)
        )
        if late:
            incidents.append("Llegada tarde")

    if clock_out is None:
        if day_complete:
            incidents.append("No checó salida")
    else:
        scheduled_end = (
            time(17, 0) if work_date.weekday() < 5 else time(13, 0)
        )
        if clock_out < scheduled_end:
            incidents.append("Salida temprana")
    return incidents


def authorizations_for_week(week_start: date) -> list[dict]:
    week_end = week_start + timedelta(days=6)
    rows = get_db().execute(
        """
        SELECT a.*, u.display_name AS created_by_name
        FROM overtime_authorizations a
        JOIN users u ON u.id = a.created_by
        WHERE a.work_date BETWEEN ? AND ?
        ORDER BY
            a.work_date, a.employee_name_key,
            a.allowed_start, a.allowed_end, a.id
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def vacations_for_range(start_date: date, end_date: date) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT v.*, u.display_name AS created_by_name
        FROM vacations v
        JOIN users u ON u.id = v.created_by
        WHERE v.start_date <= ? AND v.end_date >= ?
        ORDER BY v.start_date, v.employee_name_key, v.id
        """,
        (end_date.isoformat(), start_date.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def vacations_for_week(week_start: date) -> list[dict]:
    return vacations_for_range(week_start, week_start + timedelta(days=6))


def attendance_permissions_for_week(week_start: date) -> list[dict]:
    week_end = week_start + timedelta(days=6)
    rows = get_db().execute(
        """
        SELECT employee_name_key, work_date
        FROM attendance_permissions
        WHERE work_date BETWEEN ? AND ?
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def authorization_calendar_for_week(
    week_start: date,
    force: bool = False,
) -> tuple[list[dict], list[date]]:
    week_days = [
        week_start + timedelta(days=offset) for offset in range(7)
    ]
    authorizations = authorizations_for_week(week_start)
    people: dict[str, str] = {
        row["employee_name_key"]: row["employee_name"]
        for row in authorizations
    }

    for work_date in week_days:
        for row in cached_attendance(work_date, force=force):
            people[row["employee_name_key"]] = row["employee_name"]

    authorization_map: dict[tuple[str, str], list[dict]] = {}
    for row in authorizations:
        authorization_map.setdefault(
            (row["employee_name_key"], row["work_date"]),
            [],
        ).append(row)
    calendar_rows = []
    for name_key, employee_name in sorted(people.items()):
        calendar_rows.append(
            {
                "employee_name": employee_name,
                "employee_name_key": name_key,
                "cells": [
                    {
                        "work_date": work_date,
                        "authorizations": authorization_map.get(
                            (name_key, work_date.isoformat()),
                            [],
                        ),
                    }
                    for work_date in week_days
                ],
            }
        )
    return calendar_rows, week_days


def report_for_week(
    week_start: date,
    force: bool = False,
) -> tuple[list[dict], datetime, set[date]]:
    attendance = []
    for offset in range(7):
        attendance.extend(
            cached_attendance(
                week_start + timedelta(days=offset),
                force=force,
            )
        )
    rows = build_weekly_report(
        attendance,
        authorizations_for_week(week_start),
    )
    attendance_dates = {row["work_date"] for row in attendance}
    return rows, local_now(), attendance_dates


def weekly_report_calendar(
    report_rows: list[dict],
    week_start: date,
    vacation_rows: list[dict] | None = None,
    permission_rows: list[dict] | None = None,
) -> tuple[list[dict], list[date]]:
    week_days = [
        week_start + timedelta(days=offset) for offset in range(7)
    ]
    people: dict[str, str] = {
        normalize_name(employee_name): employee_name
        for employee_name in EMPLOYEE_DIRECTORY
    }
    people.update(
        {
            row["employee_name_key"]: row["employee_name"]
            for row in employee_directory()
        }
    )
    report_map: dict[tuple[str, date], dict] = {}
    for row in report_rows:
        name_key = row["employee_name_key"]
        people[name_key] = row["employee_name"]
        report_map[(name_key, row["work_date"])] = row
    vacation_map: dict[tuple[str, date], dict] = {}
    for vacation in vacation_rows or []:
        vacation_start = date.fromisoformat(vacation["start_date"])
        vacation_end = date.fromisoformat(vacation["end_date"])
        for offset in range((vacation_end - vacation_start).days + 1):
            vacation_date = vacation_start + timedelta(days=offset)
            if (
                week_start <= vacation_date <= week_start + timedelta(days=6)
                and vacation_date.weekday() != 6
            ):
                vacation_map[(vacation["employee_name_key"], vacation_date)] = (
                    vacation
                )
    permission_dates = {
        (
            row["employee_name_key"],
            date.fromisoformat(row["work_date"])
            if isinstance(row["work_date"], str)
            else row["work_date"],
        )
        for row in permission_rows or []
    }

    calendar_rows = []
    for name_key, employee_name in sorted(people.items()):
        cells = []
        weekly_overtime_used = 0
        for work_date in week_days:
            report = report_map.get((name_key, work_date))
            vacation = vacation_map.get((name_key, work_date))
            if report:
                counted_overtime_minutes = int(
                    report.get("authorized_minutes", 0)
                )
                report["counted_overtime_minutes"] = (
                    counted_overtime_minutes
                )
                double_minutes = min(
                    counted_overtime_minutes,
                    max(0, 9 * 60 - weekly_overtime_used),
                )
                report["double_minutes"] = double_minutes
                report["triple_minutes"] = max(
                    0,
                    counted_overtime_minutes - double_minutes,
                )
                report["rounded_counted_overtime_minutes"] = (
                    rounded_overtime_minutes(counted_overtime_minutes)
                )
                report["rounded_approved_minutes"] = (
                    rounded_overtime_minutes(
                        int(report.get("approved_minutes", 0))
                    )
                )
                report["rounded_double_minutes"] = (
                    rounded_overtime_minutes(report["double_minutes"])
                )
                report["rounded_triple_minutes"] = (
                    rounded_overtime_minutes(report["triple_minutes"])
                )
                weekly_overtime_used += counted_overtime_minutes
            today = local_today()
            is_today = work_date == today
            is_future = work_date > today
            has_incident_permission = (name_key, work_date) in permission_dates
            is_non_working = (
                work_date.weekday() == 6
                and report is None
                and vacation is None
            )
            is_incomplete = (
                not is_future
                and not is_non_working
                and vacation is None
                and not has_incident_permission
                and (
                    report is None
                    or not report.get("clock_in")
                    or not report.get("clock_out")
                )
            )
            incident_labels = []
            if (
                not is_future
                and not is_non_working
                and vacation is None
                and not has_incident_permission
            ):
                incident_labels = attendance_incident_labels(
                    work_date,
                    report,
                    day_complete=not is_today,
                )
            cells.append(
                {
                    "work_date": work_date,
                    "report": report,
                    "is_today": is_today,
                    "is_future": is_future,
                    "is_non_working": is_non_working,
                    "is_incomplete": is_incomplete,
                    "incident_labels": incident_labels,
                    "has_attendance_incident": bool(incident_labels),
                    "has_incident_permission": has_incident_permission,
                    "is_vacation": vacation is not None,
                    "vacation": vacation,
                }
            )

        calendar_rows.append(
            {
                "employee_name": employee_name,
                "employee_name_key": name_key,
                "photo_filename": employee_photo_filename(employee_name),
                "cells": cells,
                "has_preview_incidents": any(
                    (cell["is_vacation"] and not cell["has_incident_permission"])
                    or cell["has_attendance_incident"]
                    or int((cell["report"] or {}).get(
                        "rounded_counted_overtime_minutes", 0
                    )) > 0
                    for cell in cells
                ),
                "authorized_minutes": sum(
                    int((cell["report"] or {}).get(
                        "authorized_minutes", 0
                    ))
                    for cell in cells
                ),
                "unauthorized_minutes": sum(
                    int((cell["report"] or {}).get(
                        "unauthorized_minutes", 0
                    ))
                    for cell in cells
                ),
            }
        )
    return calendar_rows, week_days


def register_routes(app: Flask) -> None:
    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/setup", methods=("GET", "POST"))
    def setup():
        if user_count() > 0:
            return redirect(url_for("login"))
        if request.method == "POST":
            validate_csrf()
            display_name = request.form.get("display_name", "").strip()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")

            errors = []
            if len(display_name) < 2:
                errors.append("Escribe el nombre del administrador.")
            if len(username) < 3:
                errors.append("El usuario debe tener al menos 3 caracteres.")
            if len(password) < 10:
                errors.append("La contraseña debe tener al menos 10 caracteres.")
            if password != confirmation:
                errors.append("Las contraseñas no coinciden.")

            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                connection = get_db()
                created_at = utc_now()
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        username, display_name, password_hash,
                        supervised_area, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        display_name,
                        generate_password_hash(
                            password, method="pbkdf2:sha256"
                        ),
                        default_supervised_area(display_name),
                        created_at,
                    ),
                )
                log_action(
                    cursor.lastrowid,
                    "create",
                    "user",
                    cursor.lastrowid,
                    "Administrador inicial",
                )
                connection.commit()
                flash("Administrador creado. Ya puedes iniciar sesión.", "success")
                return redirect(url_for("login"))

        return render_template("setup.html")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if user_count() == 0:
            return redirect(url_for("setup"))
        if g.user:
            return redirect(url_for("home"))

        if request.method == "POST":
            validate_csrf()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = get_db().execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
            if (
                user is None
                or not user["active"]
                or not check_password_hash(user["password_hash"], password)
            ):
                flash("Usuario o contraseña incorrectos.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                if user["must_change_password"]:
                    return redirect(url_for("account"))
                next_url = request.args.get("next", "")
                if not next_url.startswith("/"):
                    next_url = url_for("home")
                return redirect(next_url)
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/fotos-trabajadores/<path:filename>")
    @login_required
    def employee_photo(filename: str):
        if Path(filename).suffix.casefold() not in PHOTO_EXTENSIONS:
            abort(404)
        if g.user["access_role"] == "worker":
            employee = get_db().execute(
                """
                SELECT employee_name FROM employees
                WHERE employee_name_key = ?
                """,
                (g.user["employee_name_key"],),
            ).fetchone()
            allowed_filename = (
                employee_photo_filename(employee["employee_name"])
                if employee is not None
                else None
            )
            if not allowed_filename or filename != allowed_filename:
                abort(403)
        return send_from_directory(PHOTO_DIRECTORY, filename, conditional=True)

    @app.get("/")
    @login_required
    def home():
        requested = request.args.get("semana", local_today().isoformat())
        try:
            week_start = week_start_for(parse_iso_date(requested, "semana"))
        except ValueError:
            week_start = week_start_for(local_today())
        week_end = week_start + timedelta(days=6)
        calendar_rows = []
        week_days = [week_start + timedelta(days=offset) for offset in range(7)]
        permission_rows = attendance_permissions_for_week(week_start)
        loaded_at = None
        error = None
        try:
            report_rows, loaded_at, _ = report_for_week(
                week_start,
                force=request.args.get("actualizar") == "1",
            )
            calendar_rows, week_days = weekly_report_calendar(
                report_rows,
                week_start,
                vacations_for_week(week_start),
                permission_rows,
            )
        except HikConnectError as exc:
            error = str(exc)
            calendar_rows, week_days = weekly_report_calendar(
                [], week_start, vacations_for_week(week_start), permission_rows
            )
        directory = {
            row["employee_name_key"]: row
            for row in employee_directory()
        }
        for worker in calendar_rows:
            employee = directory.get(worker["employee_name_key"], {})
            worker["area"] = employee.get("area", "")
            worker["employee_code"] = employee.get("employee_code", "")
            worker["vacation_days_available"] = employee.get(
                "vacation_days_available"
            )
            worker["vacation_balance_as_of"] = employee.get(
                "vacation_balance_as_of"
            )
            worker["vacation_synced_at"] = employee.get(
                "vacation_synced_at"
            )
            worker["vacation_source"] = employee.get(
                "vacation_source", ""
            )
        calendar_rows = [
            worker
            for worker in calendar_rows
            if not is_inactive_group(worker["area"])
        ]
        supervisor_area = str(g.user["supervised_area"] or "").strip()
        worker_mode = g.user["access_role"] == "worker"
        worker_employee_key = str(g.user["employee_name_key"] or "").strip()
        if worker_mode:
            calendar_rows = [
                worker
                for worker in calendar_rows
                if worker["employee_name_key"] == worker_employee_key
            ]
        elif supervisor_area:
            calendar_rows = [
                worker
                for worker in calendar_rows
                if same_group(worker["area"], supervisor_area)
            ]
        return render_template(
            "home.html",
            calendar_rows=calendar_rows,
            week_days=week_days,
            week_start=week_start,
            week_end=week_end,
            previous_week=week_start - timedelta(days=7),
            next_week=week_start + timedelta(days=7),
            loaded_at=loaded_at,
            error=error,
            employee_areas=[
                group
                for group in employee_groups()
                if not is_inactive_group(group)
            ],
            selected_worker=(
                worker_employee_key
                if worker_mode
                else request.args.get("trabajador", "")
            ),
            supervisor_area=supervisor_area,
            worker_mode=worker_mode,
        )

    @app.post("/permisos-incidencia")
    @login_required
    def toggle_attendance_permission():
        validate_csrf()
        employee_key = request.form.get("employee_name_key", "").strip()
        try:
            work_date = parse_iso_date(
                request.form.get("work_date", ""), "fecha"
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("home"))

        connection = get_db()
        employee = connection.execute(
            """
            SELECT employee_name, employee_name_key, area
            FROM employees WHERE employee_name_key = ?
            """,
            (employee_key,),
        ).fetchone()
        if employee is None or is_inactive_group(employee["area"]):
            abort(400, "El trabajador seleccionado no es válido.")
        supervised_area = str(g.user["supervised_area"] or "").strip()
        if supervised_area and not same_group(employee["area"], supervised_area):
            abort(403)

        has_permission = request.form.get("has_permission") == "1"
        if has_permission:
            connection.execute(
                """
                INSERT INTO attendance_permissions (
                    employee_name_key, work_date, granted_by, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(employee_name_key, work_date) DO UPDATE SET
                    granted_by = excluded.granted_by,
                    created_at = excluded.created_at
                """,
                (
                    employee_key,
                    work_date.isoformat(),
                    g.user["id"],
                    utc_now(),
                ),
            )
        else:
            connection.execute(
                """
                DELETE FROM attendance_permissions
                WHERE employee_name_key = ? AND work_date = ?
                """,
                (employee_key, work_date.isoformat()),
            )
        log_action(
            g.user["id"],
            "grant" if has_permission else "revoke",
            "attendance_permission",
            details=json.dumps(
                {
                    "employee": employee["employee_name"],
                    "work_date": work_date.isoformat(),
                },
                ensure_ascii=False,
            ),
        )
        connection.commit()
        return redirect(
            url_for(
                "home",
                semana=week_start_for(work_date),
                trabajador=employee_key,
            )
        )

    @app.post("/autorizaciones/desde-inicio")
    @login_required
    def create_home_authorization():
        validate_csrf()
        employee_key = request.form.get("employee_name_key", "").strip()
        work_date_value = request.form.get("work_date", "").strip()
        allowed_start = request.form.get("allowed_start", "").strip()
        allowed_end = request.form.get("allowed_end", "").strip()
        approved_hours_value = request.form.get("approved_hours", "").strip()

        errors = []
        try:
            work_date = parse_iso_date(work_date_value)
        except ValueError as exc:
            errors.append(str(exc))
            work_date = local_today()
        try:
            window_minutes = authorized_window_minutes(
                allowed_start,
                allowed_end,
            )
        except ValueError as exc:
            errors.append(str(exc))
            window_minutes = 0
        try:
            approved_minutes = int(
                Decimal(approved_hours_value) * Decimal(60)
            )
            if approved_minutes <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, OverflowError):
            errors.append("Indica una cantidad válida de horas aprobadas.")
            approved_minutes = 0
        if window_minutes and approved_minutes > window_minutes:
            errors.append(
                "Las horas aprobadas no pueden superar el horario habilitado."
            )

        connection = get_db()
        employee = connection.execute(
            """
            SELECT employee_name, employee_name_key
            FROM employees
            WHERE employee_name_key = ?
            """,
            (employee_key,),
        ).fetchone()
        if employee is None:
            errors.append("El trabajador seleccionado ya no está disponible.")

        if errors:
            for error in errors:
                flash(error, "error")
            return redirect(
                url_for(
                    "home",
                    semana=work_date.isoformat(),
                    trabajador=employee_key,
                )
            )

        now = utc_now()
        existing = connection.execute(
            """
            SELECT id FROM overtime_authorizations
            WHERE employee_name_key = ? AND work_date = ?
              AND allowed_start = ? AND allowed_end = ?
            ORDER BY id LIMIT 1
            """,
            (
                employee_key,
                work_date.isoformat(),
                allowed_start,
                allowed_end,
            ),
        ).fetchone()
        if existing:
            authorization_id = existing["id"]
            connection.execute(
                """
                UPDATE overtime_authorizations
                SET approved_minutes = ?, updated_at = ?
                WHERE id = ?
                """,
                (approved_minutes, now, authorization_id),
            )
            action = "update"
        else:
            cursor = connection.execute(
                """
                INSERT INTO overtime_authorizations (
                    employee_name, employee_name_key, work_date,
                    allowed_start, allowed_end, approved_minutes,
                    note, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    employee["employee_name"],
                    employee_key,
                    work_date.isoformat(),
                    allowed_start,
                    allowed_end,
                    approved_minutes,
                    g.user["id"],
                    now,
                    now,
                ),
            )
            authorization_id = cursor.lastrowid
            action = "create"
        log_action(
            g.user["id"],
            action,
            "authorization",
            authorization_id,
            json.dumps(
                {
                    "employee": employee["employee_name"],
                    "date": work_date.isoformat(),
                    "start": allowed_start,
                    "end": allowed_end,
                    "approved_minutes": approved_minutes,
                },
                ensure_ascii=False,
            ),
        )
        connection.commit()
        flash("El tiempo extra fue habilitado.", "success")
        return redirect(
            url_for(
                "home",
                semana=work_date.isoformat(),
                trabajador=employee_key,
            )
        )

    @app.post("/autorizaciones/desde-inicio/eliminar")
    @login_required
    def delete_home_authorizations():
        validate_csrf()
        employee_key = request.form.get("employee_name_key", "").strip()
        try:
            work_date = parse_iso_date(
                request.form.get("work_date", "").strip()
            )
        except ValueError:
            abort(400, "La fecha de la autorización no es válida.")

        connection = get_db()
        rows = connection.execute(
            """
            SELECT id, employee_name, allowed_start, allowed_end
            FROM overtime_authorizations
            WHERE employee_name_key = ? AND work_date = ?
            """,
            (employee_key, work_date.isoformat()),
        ).fetchall()
        if rows:
            connection.execute(
                """
                DELETE FROM overtime_authorizations
                WHERE employee_name_key = ? AND work_date = ?
                """,
                (employee_key, work_date.isoformat()),
            )
            log_action(
                g.user["id"],
                "delete",
                "authorizations",
                details=json.dumps(
                    {
                        "employee": rows[0]["employee_name"],
                        "date": work_date.isoformat(),
                        "intervals": [
                            {
                                "start": row["allowed_start"],
                                "end": row["allowed_end"],
                            }
                            for row in rows
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
            connection.commit()
            flash("El tiempo extra autorizado fue eliminado.", "success")
        else:
            flash("La autorización ya no estaba disponible.", "error")
        return redirect(
            url_for(
                "home",
                semana=work_date.isoformat(),
                trabajador=employee_key,
            )
        )

    @app.get("/trabajadores")
    @login_required
    def employees():
        error = None
        if request.args.get("actualizar") == "1":
            try:
                week_start = week_start_for(local_today())
                for offset in range(7):
                    cached_attendance(
                        week_start + timedelta(days=offset),
                        force=True,
                    )
                flash("El catálogo de trabajadores fue actualizado.", "success")
                return redirect(url_for("employees"))
            except HikConnectError as exc:
                error = str(exc)
        return render_template(
            "employees.html",
            employees=employee_directory(),
            employee_areas=employee_groups(),
            error=error,
        )

    @app.route("/solicitar-vacaciones", methods=("GET", "POST"))
    @login_required
    def request_vacation():
        if g.user["access_role"] != "worker":
            abort(403)
        connection = get_db()
        employee = connection.execute(
            """
            SELECT employee_name, employee_name_key, employee_code, area,
                   vacation_days_available, vacation_balance_as_of
            FROM employees
            WHERE employee_name_key = ?
            """,
            (g.user["employee_name_key"],),
        ).fetchone()
        if employee is None:
            abort(400, "La cuenta no tiene un trabajador vinculado.")
        vacation_movements = connection.execute(
            """
            SELECT source_movement_key, concept, registered_date,
                   start_date, end_date, days_taken, days_entitled,
                   balance
            FROM employee_vacation_movements
            WHERE employee_name_key = ?
            ORDER BY
                CASE WHEN registered_date IS NULL AND start_date IS NULL
                     THEN 0 ELSE 1 END,
                COALESCE(start_date, registered_date, ''),
                source_movement_key
            """,
            (employee["employee_name_key"],),
        ).fetchall()
        supervisors = available_supervisors()
        default_supervisor = supervisor_for_employee(employee["area"])

        if request.method == "POST":
            validate_csrf()
            selected_supervisor = request.form.get(
                "supervisor_id", ""
            ).strip()
            supervisors_by_id = {
                str(supervisor["id"]): supervisor
                for supervisor in supervisors
            }
            if selected_supervisor == "all":
                recipients = supervisors
                sent_to_all = 1
            else:
                selected = supervisors_by_id.get(selected_supervisor)
                recipients = [selected] if selected is not None else []
                sent_to_all = 0
            if not recipients:
                flash(
                    "Selecciona al menos un supervisor disponible.",
                    "error",
                )
                return redirect(url_for("request_vacation"))
            try:
                start_date = parse_iso_date(
                    request.form.get("start_date", ""), "fecha inicial"
                )
                end_date = parse_iso_date(
                    request.form.get("end_date", ""), "fecha final"
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("request_vacation"))
            if start_date < local_today():
                flash("La solicitud debe comenzar hoy o después.", "error")
                return redirect(url_for("request_vacation"))
            if end_date < start_date:
                flash(
                    "La fecha final no puede ser anterior a la inicial.",
                    "error",
                )
                return redirect(url_for("request_vacation"))
            requested_days = requested_vacation_days(start_date, end_date)
            if requested_days < 1:
                flash("El periodo debe incluir al menos un día laborable.", "error")
                return redirect(url_for("request_vacation"))
            overlapping = connection.execute(
                """
                SELECT id FROM vacation_requests
                WHERE employee_name_key = ?
                  AND status IN ('pending', 'approved')
                  AND NOT (end_date < ? OR start_date > ?)
                LIMIT 1
                """,
                (
                    employee["employee_name_key"],
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchone()
            if overlapping is not None:
                flash(
                    "Ya existe una solicitud pendiente o aprobada en ese periodo.",
                    "error",
                )
                return redirect(url_for("request_vacation"))

            cursor = connection.execute(
                """
                INSERT INTO vacation_requests (
                    employee_name, employee_name_key, supervisor_id,
                    start_date, end_date, requested_days, status,
                    requested_at, sent_to_all
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    employee["employee_name"],
                    employee["employee_name_key"],
                    recipients[0]["id"],
                    start_date.isoformat(),
                    end_date.isoformat(),
                    requested_days,
                    utc_now(),
                    sent_to_all,
                ),
            )
            connection.executemany(
                """
                INSERT INTO vacation_request_recipients (
                    request_id, supervisor_id
                ) VALUES (?, ?)
                """,
                [
                    (cursor.lastrowid, recipient["id"])
                    for recipient in recipients
                ],
            )
            log_action(
                g.user["id"],
                "create",
                "vacation_request",
                cursor.lastrowid,
                json.dumps(
                    {
                        "start": start_date.isoformat(),
                        "end": end_date.isoformat(),
                        "days": requested_days,
                        "supervisor_ids": [
                            recipient["id"] for recipient in recipients
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
            connection.commit()
            flash("Tu solicitud fue enviada al supervisor.", "success")
            return redirect(url_for("mailbox"))

        return render_template(
            "vacation_request.html",
            employee=employee,
            vacation_movements=vacation_movements,
            supervisors=supervisors,
            default_supervisor=default_supervisor,
        )

    @app.get("/buzon")
    @login_required
    def mailbox():
        connection = get_db()
        worker_mode = g.user["access_role"] == "worker"
        if worker_mode:
            connection.execute(
                """
                UPDATE vacation_requests
                SET worker_read_at = ?
                WHERE employee_name_key = ?
                  AND status IN ('approved', 'rejected')
                  AND worker_read_at IS NULL
                  AND worker_deleted_at IS NULL
                """,
                (utc_now(), g.user["employee_name_key"]),
            )
            connection.commit()
            rows = connection.execute(
                """
                SELECT vr.*,
                       CASE
                         WHEN vr.sent_to_all = 1 THEN 'Todos los supervisores'
                         ELSE (
                           SELECT GROUP_CONCAT(u.display_name, ', ')
                           FROM vacation_request_recipients recipient
                           JOIN users u ON u.id = recipient.supervisor_id
                           WHERE recipient.request_id = vr.id
                         )
                       END AS supervisor_name,
                       decision.display_name AS decided_by_name
                FROM vacation_requests vr
                LEFT JOIN users decision ON decision.id = vr.decided_by
                WHERE vr.employee_name_key = ?
                  AND vr.worker_deleted_at IS NULL
                ORDER BY vr.requested_at DESC, vr.id DESC
                """,
                (g.user["employee_name_key"],),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT vr.*,
                       CASE
                         WHEN vr.sent_to_all = 1 THEN 'Todos los supervisores'
                         ELSE (
                           SELECT GROUP_CONCAT(u.display_name, ', ')
                           FROM vacation_request_recipients recipient_names
                           JOIN users u ON u.id = recipient_names.supervisor_id
                           WHERE recipient_names.request_id = vr.id
                         )
                       END AS supervisor_name,
                       decision.display_name AS decided_by_name
                FROM vacation_requests vr
                JOIN vacation_request_recipients recipient
                  ON recipient.request_id = vr.id
                LEFT JOIN users decision ON decision.id = vr.decided_by
                WHERE recipient.supervisor_id = ?
                  AND recipient.deleted_at IS NULL
                ORDER BY CASE vr.status WHEN 'pending' THEN 0 ELSE 1 END,
                         vr.requested_at DESC, vr.id DESC
                """,
                (g.user["id"],),
            ).fetchall()
        return render_template(
            "mailbox.html",
            rows=rows,
            worker_mode=worker_mode,
            pending_count=sum(row["status"] == "pending" for row in rows),
            resolved_count=sum(row["status"] != "pending" for row in rows),
        )

    @app.post("/buzon/solicitudes/<int:request_id>/decision")
    @login_required
    def decide_vacation_request(request_id: int):
        validate_csrf()
        decision = request.form.get("decision", "").strip()
        if decision not in {"approved", "rejected"}:
            abort(400, "La decisión no es válida.")
        connection = get_db()
        vacation_request = connection.execute(
            """
            SELECT vr.* FROM vacation_requests vr
            JOIN vacation_request_recipients recipient
              ON recipient.request_id = vr.id
            WHERE vr.id = ? AND recipient.supervisor_id = ?
              AND recipient.deleted_at IS NULL
            """,
            (request_id, g.user["id"]),
        ).fetchone()
        if vacation_request is None:
            abort(404)
        if vacation_request["status"] != "pending":
            flash("Esa solicitud ya fue respondida.", "error")
            return redirect(url_for("mailbox"))
        responded_at = utc_now()
        connection.execute(
            """
            UPDATE vacation_requests
            SET status = ?, responded_at = ?, decided_by = ?,
                worker_read_at = NULL,
                contpaqi_status = ?, contpaqi_error = '',
                contpaqi_locked_at = NULL, contpaqi_updated_at = ?
            WHERE id = ?
            """,
            (
                decision,
                responded_at,
                g.user["id"],
                "pending" if decision == "approved" else "not_queued",
                responded_at,
                request_id,
            ),
        )
        log_action(
            g.user["id"],
            decision,
            "vacation_request",
            request_id,
            vacation_request["employee_name"],
        )
        connection.commit()
        flash(
            "La solicitud fue aprobada."
            if decision == "approved"
            else "La solicitud fue rechazada.",
            "success",
        )
        return redirect(url_for("mailbox"))

    @app.post("/buzon/solicitudes/<int:request_id>/reintentar-contpaqi")
    @login_required
    def retry_contpaqi_vacation_request(request_id: int):
        validate_csrf()
        if g.user["access_role"] == "worker":
            abort(403)
        connection = get_db()
        vacation_request = connection.execute(
            """
            SELECT vr.id FROM vacation_requests vr
            JOIN vacation_request_recipients recipient
              ON recipient.request_id = vr.id
            WHERE vr.id = ? AND recipient.supervisor_id = ?
              AND vr.status = 'approved'
              AND vr.contpaqi_status = 'failed'
            """,
            (request_id, g.user["id"]),
        ).fetchone()
        if vacation_request is None:
            abort(404)
        now = utc_now()
        connection.execute(
            """
            UPDATE vacation_requests
            SET contpaqi_status = 'pending', contpaqi_error = '',
                contpaqi_locked_at = NULL, contpaqi_updated_at = ?
            WHERE id = ?
            """,
            (now, request_id),
        )
        log_action(
            g.user["id"], "retry", "contpaqi_vacation_request", request_id
        )
        connection.commit()
        flash("La aplicación en CONTPAQi quedó lista para reintentarse.", "success")
        return redirect(url_for("mailbox"))

    @app.post("/buzon/solicitudes/<int:request_id>/eliminar")
    @login_required
    def delete_vacation_request(request_id: int):
        validate_csrf()
        connection = get_db()
        if g.user["access_role"] == "worker":
            vacation_request = connection.execute(
                """
                SELECT id, status FROM vacation_requests
                WHERE id = ? AND employee_name_key = ?
                  AND worker_deleted_at IS NULL
                """,
                (request_id, g.user["employee_name_key"]),
            ).fetchone()
            if vacation_request is None:
                abort(404)
            if vacation_request["status"] == "pending":
                abort(400, "No puedes eliminar una solicitud pendiente.")
            connection.execute(
                """
                UPDATE vacation_requests SET worker_deleted_at = ?
                WHERE id = ?
                """,
                (utc_now(), request_id),
            )
        else:
            vacation_request = connection.execute(
                """
                SELECT vr.status FROM vacation_requests vr
                JOIN vacation_request_recipients recipient
                  ON recipient.request_id = vr.id
                WHERE vr.id = ? AND recipient.supervisor_id = ?
                  AND recipient.deleted_at IS NULL
                """,
                (request_id, g.user["id"]),
            ).fetchone()
            if vacation_request is None:
                abort(404)
            if vacation_request["status"] == "pending":
                abort(400, "No puedes eliminar una solicitud pendiente.")
            connection.execute(
                """
                UPDATE vacation_request_recipients SET deleted_at = ?
                WHERE request_id = ? AND supervisor_id = ?
                """,
                (utc_now(), request_id, g.user["id"]),
            )
        log_action(
            g.user["id"],
            "delete_from_mailbox",
            "vacation_request",
            request_id,
        )
        connection.commit()
        flash("El mensaje fue eliminado de tu buzón.", "success")
        return redirect(url_for("mailbox"))

    @app.post("/buzon/limpiar")
    @login_required
    def clear_mailbox():
        validate_csrf()
        connection = get_db()
        now = utc_now()
        if g.user["access_role"] == "worker":
            connection.execute(
                """
                UPDATE vacation_requests SET worker_deleted_at = ?
                WHERE employee_name_key = ?
                  AND status IN ('approved', 'rejected')
                  AND worker_deleted_at IS NULL
                """,
                (now, g.user["employee_name_key"]),
            )
        else:
            connection.execute(
                """
                UPDATE vacation_request_recipients
                SET deleted_at = ?
                WHERE supervisor_id = ? AND deleted_at IS NULL
                  AND request_id IN (
                    SELECT id FROM vacation_requests
                    WHERE status IN ('approved', 'rejected')
                  )
                """,
                (now, g.user["id"]),
            )
        log_action(g.user["id"], "clear", "vacation_request_mailbox")
        connection.commit()
        flash("Los mensajes resueltos fueron eliminados.", "success")
        return redirect(url_for("mailbox"))

    @app.get("/vacaciones")
    @login_required
    def vacations():
        active_groups = [
            group for group in employee_groups()
            if not is_inactive_group(group)
        ]
        supervised_area = str(g.user["supervised_area"] or "").strip()
        if supervised_area:
            available_groups = [supervised_area]
            selected_group = supervised_area
        else:
            available_groups = active_groups
            selected_group = request.args.get("grupo", "").strip()
            if selected_group and selected_group not in active_groups:
                abort(400, "El grupo seleccionado no es válido.")
            if not selected_group and active_groups:
                selected_group = active_groups[0]

        workers = [
            worker for worker in employee_directory()
            if selected_group and same_group(worker["area"], selected_group)
        ]
        for worker in workers:
            worker["photo_filename"] = employee_photo_filename(
                worker["employee_name"]
            )
        connection = get_db()
        today_iso = local_today().isoformat()
        vacation_rows = connection.execute(
            """
            SELECT v.*, e.area, u.display_name AS created_by_name
            FROM vacations v
            LEFT JOIN employees e
              ON e.employee_name_key = v.employee_name_key
            JOIN users u ON u.id = v.created_by
            WHERE (? = '' OR e.area = ? COLLATE NOCASE)
              AND v.end_date >= ?
            ORDER BY v.start_date, v.employee_name_key
            """,
            (selected_group, selected_group, today_iso),
        ).fetchall()
        vacation_history_rows = connection.execute(
            """
            SELECT v.*, e.area, u.display_name AS created_by_name
            FROM vacations v
            LEFT JOIN employees e
              ON e.employee_name_key = v.employee_name_key
            JOIN users u ON u.id = v.created_by
            WHERE (? = '' OR e.area = ? COLLATE NOCASE)
              AND v.end_date < ?
            ORDER BY v.end_date DESC, v.employee_name_key
            """,
            (selected_group, selected_group, today_iso),
        ).fetchall()
        return render_template(
            "vacations.html",
            employees=workers,
            employee_areas=available_groups,
            selected_group=selected_group,
            vacation_rows=vacation_rows,
            vacation_history_rows=vacation_history_rows,
        )

    @app.post("/vacaciones/nueva")
    @login_required
    def create_vacation():
        validate_csrf()
        employee_key = request.form.get("employee_name_key", "").strip()
        group_name = request.form.get("group_name", "").strip()
        try:
            start_date = parse_iso_date(
                request.form.get("start_date", ""), "fecha inicial"
            )
            end_date = parse_iso_date(
                request.form.get("end_date", ""), "fecha final"
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("vacations", grupo=group_name))
        if end_date < start_date:
            flash("La fecha final no puede ser anterior a la inicial.", "error")
            return redirect(url_for("vacations", grupo=group_name))

        connection = get_db()
        employee = connection.execute(
            """
            SELECT employee_name, employee_name_key, area
            FROM employees WHERE employee_name_key = ?
            """,
            (employee_key,),
        ).fetchone()
        if employee is None or is_inactive_group(employee["area"]):
            abort(400, "El trabajador seleccionado no es válido.")
        supervised_area = str(g.user["supervised_area"] or "").strip()
        if supervised_area and not same_group(employee["area"], supervised_area):
            abort(403)

        overlap = connection.execute(
            """
            SELECT id FROM vacations
            WHERE employee_name_key = ?
              AND start_date <= ? AND end_date >= ?
            LIMIT 1
            """,
            (employee_key, end_date.isoformat(), start_date.isoformat()),
        ).fetchone()
        if overlap:
            flash(
                "Ese trabajador ya tiene vacaciones en parte de ese periodo.",
                "error",
            )
            return redirect(url_for("vacations", grupo=employee["area"]))

        cursor = connection.execute(
            """
            INSERT INTO vacations (
                employee_name, employee_name_key, start_date, end_date,
                created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                employee["employee_name"],
                employee_key,
                start_date.isoformat(),
                end_date.isoformat(),
                g.user["id"],
                utc_now(),
            ),
        )
        log_action(
            g.user["id"],
            "create",
            "vacation",
            cursor.lastrowid,
            json.dumps(
                {
                    "employee": employee["employee_name"],
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                },
                ensure_ascii=False,
            ),
        )
        connection.commit()
        flash("El periodo de vacaciones fue guardado.", "success")
        return redirect(url_for("vacations", grupo=employee["area"]))

    @app.post("/vacaciones/<int:vacation_id>/eliminar")
    @login_required
    def delete_vacation(vacation_id: int):
        validate_csrf()
        connection = get_db()
        vacation = connection.execute(
            """
            SELECT v.*, e.area FROM vacations v
            LEFT JOIN employees e
              ON e.employee_name_key = v.employee_name_key
            WHERE v.id = ?
            """,
            (vacation_id,),
        ).fetchone()
        if vacation is None:
            abort(404)
        supervised_area = str(g.user["supervised_area"] or "").strip()
        if supervised_area and not same_group(
            vacation["area"] or "", supervised_area
        ):
            abort(403)
        connection.execute("DELETE FROM vacations WHERE id = ?", (vacation_id,))
        log_action(
            g.user["id"],
            "delete",
            "vacation",
            vacation_id,
            vacation["employee_name"],
        )
        connection.commit()
        flash("El periodo de vacaciones fue eliminado.", "success")
        return redirect(url_for("vacations", grupo=vacation["area"] or ""))

    @app.get("/resumen")
    @login_required
    def dashboard():
        week_start = week_start_for(local_today())
        week_end = week_start + timedelta(days=6)
        connection = get_db()
        stats = connection.execute(
            """
            WITH authorization_minutes AS (
                SELECT
                    employee_name_key,
                    approved_minutes,
                    (
                        CAST(substr(allowed_start, 1, 2) AS INTEGER) * 60
                        + CAST(substr(allowed_start, 4, 2) AS INTEGER)
                    ) AS start_minute,
                    (
                        CAST(substr(allowed_end, 1, 2) AS INTEGER) * 60
                        + CAST(substr(allowed_end, 4, 2) AS INTEGER)
                    ) AS end_minute
                FROM overtime_authorizations
                WHERE work_date BETWEEN ? AND ?
            )
            SELECT
                COUNT(*) AS total,
                COUNT(DISTINCT employee_name_key) AS employees,
                COALESCE(SUM(
                    CASE
                        WHEN approved_minutes IS NOT NULL
                        THEN approved_minutes
                        WHEN end_minute > start_minute
                        THEN end_minute - start_minute
                        ELSE end_minute + 1440 - start_minute
                    END
                ), 0) AS minutes
            FROM authorization_minutes
            """,
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchone()
        upcoming = connection.execute(
            """
            SELECT a.*, u.display_name AS created_by_name
            FROM overtime_authorizations a
            JOIN users u ON u.id = a.created_by
            WHERE a.work_date >= ?
            ORDER BY a.work_date, a.allowed_start
            LIMIT 6
            """,
            (local_today().isoformat(),),
        ).fetchall()
        return render_template(
            "dashboard.html",
            week_start=week_start,
            week_end=week_end,
            stats=stats,
            upcoming=upcoming,
        )

    @app.get("/autorizaciones")
    @login_required
    def authorizations():
        return redirect(url_for("home"))

        requested = request.args.get("semana", local_today().isoformat())
        try:
            week_start = week_start_for(parse_iso_date(requested, "semana"))
        except ValueError:
            week_start = week_start_for(local_today())
        rows = []
        week_days = [
            week_start + timedelta(days=offset) for offset in range(7)
        ]
        error = None
        try:
            rows, week_days = authorization_calendar_for_week(
                week_start,
                force=request.args.get("actualizar") == "1",
            )
        except HikConnectError as exc:
            error = str(exc)
            authorization_rows = authorizations_for_week(week_start)
            people = {
                row["employee_name_key"]: row["employee_name"]
                for row in authorization_rows
            }
            authorization_map: dict[tuple[str, str], list[dict]] = {}
            for row in authorization_rows:
                authorization_map.setdefault(
                    (row["employee_name_key"], row["work_date"]),
                    [],
                ).append(row)
            rows = [
                {
                    "employee_name": employee_name,
                    "employee_name_key": name_key,
                    "cells": [
                        {
                            "work_date": work_date,
                            "authorizations": authorization_map.get(
                                (name_key, work_date.isoformat()),
                                [],
                            ),
                        }
                        for work_date in week_days
                    ],
                }
                for name_key, employee_name in sorted(people.items())
            ]
        return render_template(
            "authorizations.html",
            rows=rows,
            week_days=week_days,
            error=error,
            week_start=week_start,
            week_end=week_start + timedelta(days=6),
            previous_week=week_start - timedelta(days=7),
            next_week=week_start + timedelta(days=7),
        )

    @app.route("/autorizaciones/nueva", methods=("GET", "POST"))
    @login_required
    def new_authorization():
        return redirect(url_for("home"))

        default_week = week_start_for(local_today())
        if request.method == "POST":
            validate_csrf()
            employee_names = [
                value.strip()
                for value in request.form.getlist("employee_names")
                if value.strip()
            ]
            manual_name = request.form.get("manual_name", "").strip()
            if manual_name:
                employee_names.append(manual_name)
            employee_names = list(
                {
                    normalize_name(name): name
                    for name in employee_names
                }.values()
            )
            work_dates = request.form.getlist("work_dates")
            allowed_start = request.form.get("allowed_start", "")
            allowed_end = request.form.get("allowed_end", "")
            note = request.form.get("note", "").strip()
            reference_date = request.form.get(
                "reference_date", local_today().isoformat()
            )

            errors = []
            if not employee_names:
                errors.append("Selecciona al menos un trabajador.")
            if not work_dates:
                errors.append("Selecciona al menos un día.")
            if not allowed_start or not allowed_end:
                errors.append("Indica el horario autorizado.")
            elif allowed_start == allowed_end:
                errors.append(
                    "La hora inicial y final deben ser diferentes."
                )

            parsed_dates = []
            for value in work_dates:
                try:
                    parsed_dates.append(parse_iso_date(value))
                except ValueError as exc:
                    errors.append(str(exc))

            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                connection = get_db()
                now = utc_now()
                affected = 0
                for employee_name in employee_names:
                    name_key = normalize_name(employee_name)
                    for work_date in parsed_dates:
                        duplicate = connection.execute(
                            """
                            SELECT id
                            FROM overtime_authorizations
                            WHERE employee_name_key = ?
                              AND work_date = ?
                              AND allowed_start = ?
                              AND allowed_end = ?
                            """,
                            (
                                name_key,
                                work_date.isoformat(),
                                allowed_start,
                                allowed_end,
                            ),
                        ).fetchone()
                        if duplicate:
                            continue
                        cursor = connection.execute(
                            """
                            INSERT INTO overtime_authorizations (
                                employee_name, employee_name_key, work_date,
                                allowed_start, allowed_end, note, created_by,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                employee_name,
                                name_key,
                                work_date.isoformat(),
                                allowed_start,
                                allowed_end,
                                note,
                                g.user["id"],
                                now,
                                now,
                            ),
                        )
                        log_action(
                            g.user["id"],
                            "create",
                            "authorization",
                            cursor.lastrowid,
                            json.dumps(
                                {
                                    "employee": employee_name,
                                    "date": work_date.isoformat(),
                                    "start": allowed_start,
                                    "end": allowed_end,
                                },
                                ensure_ascii=False,
                            ),
                        )
                        affected += 1
                connection.commit()
                if affected:
                    flash(
                        f"Se guardaron {affected} autorizaciones.",
                        "success",
                    )
                else:
                    flash(
                        "Ese horario ya estaba autorizado en los días seleccionados.",
                        "error",
                    )
                return redirect(
                    url_for(
                        "authorizations",
                        semana=min(parsed_dates).isoformat(),
                    )
                )

            try:
                default_week = week_start_for(parse_iso_date(work_dates[0]))
            except (IndexError, ValueError):
                pass
            return render_template(
                "authorization_form.html",
                week_start=default_week,
                week_days=[
                    default_week + timedelta(days=index) for index in range(7)
                ],
                reference_date=reference_date,
                submitted=request.form,
            )

        return render_template(
            "authorization_form.html",
            week_start=default_week,
            week_days=[
                default_week + timedelta(days=index) for index in range(7)
            ],
            reference_date=local_today().isoformat(),
            submitted=None,
        )

    @app.post("/autorizaciones/<int:authorization_id>/eliminar")
    @login_required
    def delete_authorization(authorization_id: int):
        return redirect(url_for("home"))

        validate_csrf()
        connection = get_db()
        row = connection.execute(
            "SELECT * FROM overtime_authorizations WHERE id = ?",
            (authorization_id,),
        ).fetchone()
        if row is None:
            abort(404)
        connection.execute(
            "DELETE FROM overtime_authorizations WHERE id = ?",
            (authorization_id,),
        )
        log_action(
            g.user["id"],
            "delete",
            "authorization",
            authorization_id,
            json.dumps(
                {
                    "employee": row["employee_name"],
                    "date": row["work_date"],
                },
                ensure_ascii=False,
            ),
        )
        connection.commit()
        flash("La autorización fue eliminada.", "success")
        return redirect(
            url_for("authorizations", semana=row["work_date"])
        )

    @app.get("/api/trabajadores")
    @login_required
    def api_workers():
        try:
            reference_date = parse_iso_date(
                request.args.get("fecha", local_today().isoformat())
            )
            cached_attendance(
                reference_date,
                force=request.args.get("actualizar") == "1",
            )
            workers = [
                {
                    "name": row["employee_name"],
                    "employeeCode": row["employee_code"],
                    "groupName": row["area"],
                }
                for row in employee_directory()
            ]
            return jsonify(
                {
                    "workers": workers,
                    "date": reference_date.isoformat(),
                }
            )
        except (ValueError, HikConnectError) as exc:
            return jsonify({"error": str(exc)}), 502

    @app.post("/api/integraciones/contpaqi/vacaciones")
    def receive_contpaqi_vacation_balances():
        if not current_app.config.get("CONTPAQ_SYNC_TOKEN"):
            return jsonify({"error": "La integración no está configurada."}), 503
        if not integration_authenticated():
            return jsonify({"error": "Credenciales de integración inválidas."}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "El cuerpo JSON no es válido."}), 400
        balances = payload.get("balances")
        if not isinstance(balances, list) or len(balances) > 2000:
            return jsonify({"error": "La lista de saldos no es válida."}), 400
        try:
            balance_as_of = date.fromisoformat(str(payload.get("asOf", "")))
        except ValueError:
            return jsonify({"error": "La fecha de corte no es válida."}), 400
        source = str(payload.get("source") or "CONTPAQi Nóminas").strip()[:80]

        connection = get_db()
        employee_rows = connection.execute(
            """
            SELECT employee_name_key, employee_name, employee_code
            FROM employees
            """
        ).fetchall()

        def code_key(value: str) -> str:
            normalized = str(value or "").strip()
            return (
                str(int(normalized))
                if normalized.isdigit()
                else normalized.casefold()
            )

        employees_by_code: dict[str, list[str]] = {}
        employees_by_name: dict[tuple[str, ...], list[str]] = {}
        employee_details = {}
        for employee in employee_rows:
            employee_key = employee["employee_name_key"]
            employee_details[employee_key] = dict(employee)
            if str(employee["employee_code"] or "").strip():
                employees_by_code.setdefault(
                    code_key(employee["employee_code"]), []
                ).append(employee_key)
            employees_by_name.setdefault(
                employee_identity_key(employee["employee_name"]), []
            ).append(employee_key)

        validated = []
        invalid = []
        unmatched = []
        matched_by_name = []
        name_mismatches = []
        for index, item in enumerate(balances):
            if not isinstance(item, dict):
                invalid.append(index)
                continue
            employee_code = str(item.get("employeeCode") or "").strip()
            employee_name = str(item.get("employeeName") or "").strip()
            try:
                available_days = float(item.get("availableDays"))
                contpaqi_employee_id = int(item.get("employeeId"))
            except (TypeError, ValueError):
                invalid.append(index)
                continue
            if (
                not employee_code
                or not math.isfinite(available_days)
                or available_days < -365
                or available_days > 3650
            ):
                invalid.append(index)
                continue
            raw_movements = item.get("movements")
            movements = None
            if raw_movements is not None:
                if not isinstance(raw_movements, list) or len(raw_movements) > 500:
                    invalid.append(index)
                    continue
                movements = []
                movement_keys = set()
                movement_is_invalid = False
                for movement in raw_movements:
                    if not isinstance(movement, dict):
                        movement_is_invalid = True
                        break
                    movement_key = str(
                        movement.get("sourceMovementKey") or ""
                    ).strip()[:100]
                    concept = str(
                        movement.get("concept") or ""
                    ).strip()[:80]
                    try:
                        days_taken = float(movement.get("daysTaken") or 0)
                        days_entitled = float(
                            movement.get("daysEntitled") or 0
                        )
                        movement_balance = float(movement.get("balance"))
                        movement_dates = {}
                        for field in (
                            "registeredDate", "startDate", "endDate"
                        ):
                            raw_date = movement.get(field)
                            movement_dates[field] = (
                                date.fromisoformat(str(raw_date)).isoformat()
                                if raw_date
                                else None
                            )
                    except (TypeError, ValueError):
                        movement_is_invalid = True
                        break
                    if (
                        not movement_key
                        or not concept
                        or movement_key in movement_keys
                        or not all(
                            math.isfinite(value)
                            and -3650 <= value <= 3650
                            for value in (
                                days_taken,
                                days_entitled,
                                movement_balance,
                            )
                        )
                    ):
                        movement_is_invalid = True
                        break
                    movement_keys.add(movement_key)
                    movements.append(
                        {
                            "source_movement_key": movement_key,
                            "concept": concept,
                            "registered_date": movement_dates["registeredDate"],
                            "start_date": movement_dates["startDate"],
                            "end_date": movement_dates["endDate"],
                            "days_taken": days_taken,
                            "days_entitled": days_entitled,
                            "balance": movement_balance,
                        }
                    )
                if movement_is_invalid:
                    invalid.append(index)
                    continue
            matches = employees_by_code.get(code_key(employee_code), [])
            matched_using_name = False
            if not matches and employee_name:
                name_matches = employees_by_name.get(
                    employee_identity_key(employee_name), []
                )
                name_matches = [
                    key
                    for key in name_matches
                    if not str(
                        employee_details[key]["employee_code"] or ""
                    ).strip()
                ]
                if len(name_matches) == 1:
                    matches = name_matches
                    matched_using_name = True
            if len(matches) != 1:
                unmatched.append(employee_code)
                continue
            employee_key = matches[0]
            if matched_using_name:
                matched_by_name.append(employee_code)
            elif (
                employee_name
                and employee_identity_key(employee_name)
                != employee_identity_key(
                    employee_details[employee_key]["employee_name"]
                )
            ):
                name_mismatches.append(employee_code)
            validated.append(
                (
                    employee_key,
                    employee_code,
                    employee_name,
                    str(item.get("employeeStatus") or "").strip()[:8],
                    contpaqi_employee_id,
                    available_days,
                    movements,
                )
            )
        if invalid:
            return jsonify(
                {
                    "error": "Hay saldos con formato inválido.",
                    "invalidRows": invalid,
                }
            ), 400

        synced_at = utc_now()
        connection.executemany(
            """
            UPDATE employees
            SET employee_code = CASE
                    WHEN TRIM(employee_code) = '' THEN ?
                    ELSE employee_code
                END,
                contpaqi_employee_name = ?,
                contpaqi_employee_status = ?,
                contpaqi_employee_id = ?,
                vacation_days_available = ?,
                vacation_balance_as_of = ?,
                vacation_synced_at = ?,
                vacation_source = ?,
                updated_at = ?
            WHERE employee_name_key = ?
            """,
            [
                (
                    employee_code,
                    employee_name,
                    employee_status,
                    contpaqi_employee_id,
                    available_days,
                    balance_as_of.isoformat(),
                    synced_at,
                    source,
                    synced_at,
                    employee_name_key,
                )
                for (
                    employee_name_key,
                    employee_code,
                    employee_name,
                    employee_status,
                    contpaqi_employee_id,
                    available_days,
                    _movements,
                ) in validated
            ],
        )
        movement_updates = 0
        for (
            employee_name_key,
            _employee_code,
            _employee_name,
            _employee_status,
            _contpaqi_employee_id,
            _available_days,
            movements,
        ) in validated:
            if movements is None:
                continue
            connection.execute(
                """
                DELETE FROM employee_vacation_movements
                WHERE employee_name_key = ?
                """,
                (employee_name_key,),
            )
            connection.executemany(
                """
                INSERT INTO employee_vacation_movements (
                    employee_name_key, source_movement_key, concept,
                    registered_date, start_date, end_date, days_taken,
                    days_entitled, balance, balance_as_of, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        employee_name_key,
                        movement["source_movement_key"],
                        movement["concept"],
                        movement["registered_date"],
                        movement["start_date"],
                        movement["end_date"],
                        movement["days_taken"],
                        movement["days_entitled"],
                        movement["balance"],
                        balance_as_of.isoformat(),
                        synced_at,
                    )
                    for movement in movements
                ],
            )
            movement_updates += len(movements)
        connection.commit()
        return jsonify(
            {
                "updated": len(validated),
                "movementsUpdated": movement_updates,
                "unmatched": len(unmatched),
                "unmatchedEmployeeCodes": unmatched,
                "matchedByName": len(matched_by_name),
                "nameMismatches": name_mismatches,
                "asOf": balance_as_of.isoformat(),
                "syncedAt": synced_at,
            }
        )

    @app.post("/api/integraciones/contpaqi/solicitudes/tomar")
    def claim_contpaqi_vacation_request():
        if not current_app.config.get("CONTPAQ_SYNC_TOKEN"):
            return jsonify({"error": "La integración no está configurada."}), 503
        if not integration_authenticated():
            return jsonify({"error": "Credenciales de integración inválidas."}), 401

        connection = get_db()
        now = utc_now()
        stale_before = (
            datetime.utcnow() - timedelta(minutes=10)
        ).replace(microsecond=0).isoformat() + "Z"
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE vacation_requests
            SET contpaqi_status = 'pending', contpaqi_locked_at = NULL,
                contpaqi_updated_at = ?
            WHERE status = 'approved'
              AND contpaqi_status = 'processing'
              AND contpaqi_locked_at < ?
            """,
            (now, stale_before),
        )
        row = connection.execute(
            """
            SELECT vr.id, vr.employee_name, vr.employee_name_key,
                   employee.employee_code, vr.start_date, vr.end_date,
                   vr.requested_days, vr.responded_at
            FROM vacation_requests vr
            LEFT JOIN employees employee
              ON employee.employee_name_key = vr.employee_name_key
            WHERE vr.status = 'approved'
              AND vr.contpaqi_status = 'pending'
            ORDER BY vr.id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            connection.commit()
            return Response(status=204)
        updated = connection.execute(
            """
            UPDATE vacation_requests
            SET contpaqi_status = 'processing',
                contpaqi_attempts = contpaqi_attempts + 1,
                contpaqi_locked_at = ?, contpaqi_updated_at = ?
            WHERE id = ? AND contpaqi_status = 'pending'
            """,
            (now, now, row["id"]),
        ).rowcount
        connection.commit()
        if updated != 1:
            return Response(status=204)
        return jsonify(
            {
                "requestId": row["id"],
                "employeeCode": row["employee_code"] or "",
                "employeeName": row["employee_name"],
                "startDate": row["start_date"],
                "endDate": row["end_date"],
                "requestedDays": row["requested_days"],
                "approvedAt": row["responded_at"],
            }
        )

    @app.post("/api/integraciones/contpaqi/solicitudes/<int:request_id>/resultado")
    def finish_contpaqi_vacation_request(request_id: int):
        if not current_app.config.get("CONTPAQ_SYNC_TOKEN"):
            return jsonify({"error": "La integración no está configurada."}), 503
        if not integration_authenticated():
            return jsonify({"error": "Credenciales de integración inválidas."}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "El cuerpo JSON no es válido."}), 400
        outcome = str(payload.get("status") or "").strip()
        if outcome not in {"applied", "failed"}:
            return jsonify({"error": "El resultado no es válido."}), 400
        record_id = payload.get("recordId")
        if outcome == "applied":
            try:
                record_id = int(record_id)
            except (TypeError, ValueError):
                return jsonify({"error": "Falta el folio de CONTPAQi."}), 400
            if record_id <= 0:
                return jsonify({"error": "El folio de CONTPAQi no es válido."}), 400
        error = str(payload.get("error") or "").strip()[:500]
        if outcome == "failed" and not error:
            error = "El conector no pudo aplicar la solicitud."
        connection = get_db()
        row = connection.execute(
            """
            SELECT status, contpaqi_status, contpaqi_record_id
            FROM vacation_requests WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            abort(404)
        if row["status"] != "approved":
            return jsonify({"error": "La solicitud no está aprobada."}), 409
        if row["contpaqi_status"] == "applied":
            if outcome == "applied" and row["contpaqi_record_id"] == record_id:
                return jsonify({"status": "applied", "idempotent": True})
            return jsonify({"error": "La solicitud ya fue aplicada."}), 409
        if row["contpaqi_status"] != "processing":
            return jsonify({"error": "La solicitud no está tomada por el conector."}), 409
        now = utc_now()
        connection.execute(
            """
            UPDATE vacation_requests
            SET contpaqi_status = ?, contpaqi_error = ?,
                contpaqi_record_id = ?, contpaqi_applied_at = ?,
                contpaqi_locked_at = NULL, contpaqi_updated_at = ?,
                worker_read_at = NULL
            WHERE id = ?
            """,
            (
                outcome,
                "" if outcome == "applied" else error,
                record_id if outcome == "applied" else None,
                now if outcome == "applied" else None,
                now,
                request_id,
            ),
        )
        connection.commit()
        return jsonify({"status": outcome})

    @app.get("/reporte")
    @login_required
    def weekly_report():
        requested = request.args.get("semana", local_today().isoformat())
        try:
            week_start = week_start_for(parse_iso_date(requested, "semana"))
        except ValueError:
            week_start = week_start_for(local_today())

        rows = []
        loaded_at = None
        attendance_dates = set()
        error = None
        try:
            rows, loaded_at, attendance_dates = report_for_week(
                week_start,
                force=request.args.get("actualizar") == "1",
            )
        except HikConnectError as exc:
            error = str(exc)

        totals = {
            "overtime": sum(row["overtime_minutes"] for row in rows),
            "authorized": sum(row["authorized_minutes"] for row in rows),
            "unauthorized": sum(row["unauthorized_minutes"] for row in rows),
        }
        calendar_rows, week_days = weekly_report_calendar(
            rows,
            week_start,
            vacations_for_week(week_start),
            attendance_permissions_for_week(week_start),
        )
        return render_template(
            "report.html",
            rows=rows,
            calendar_rows=calendar_rows,
            week_days=week_days,
            totals=totals,
            error=error,
            loaded_at=loaded_at,
            week_start=week_start,
            week_end=week_start + timedelta(days=6),
            previous_week=week_start - timedelta(days=7),
            next_week=week_start + timedelta(days=7),
            today_pending=(
                week_start <= local_today() <= week_start + timedelta(days=6)
                and local_today() not in attendance_dates
            ),
        )

    @app.get("/reporte.csv")
    @login_required
    def report_csv():
        try:
            week_start = week_start_for(
                parse_iso_date(
                    request.args.get("semana", local_today().isoformat())
                )
            )
            rows, _loaded_at, _attendance_dates = report_for_week(week_start)
        except (ValueError, HikConnectError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("weekly_report"))

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(
            [
                "Trabajador",
                "Fecha",
                "Entrada",
                "Salida",
                "Horario autorizado",
                "Horas extra detectadas",
                "Horas autorizadas",
                "Horas no autorizadas",
                "Estado",
                "Comentario",
            ]
        )
        vacation_rows = vacations_for_week(week_start)
        vacation_days = {}
        for vacation in vacation_rows:
            current = max(date.fromisoformat(vacation["start_date"]), week_start)
            final = min(
                date.fromisoformat(vacation["end_date"]),
                week_start + timedelta(days=6),
            )
            while current <= final:
                if current.weekday() != 6:
                    vacation_days[(
                        vacation["employee_name_key"], current
                    )] = vacation
                current += timedelta(days=1)

        exported_days = set()
        for row in rows:
            key = (row["employee_name_key"], row["work_date"])
            vacation = vacation_days.get(key)
            exported_days.add(key)
            writer.writerow(
                [
                    row["employee_name"],
                    row["work_date"].isoformat(),
                    row["clock_in"].strftime("%H:%M")
                    if row["clock_in"]
                    else "",
                    row["clock_out"].strftime("%H:%M")
                    if row["clock_out"]
                    else "",
                    row["allowed_range"],
                    format_minutes(row["overtime_minutes"]),
                    format_minutes(row["authorized_minutes"]),
                    format_minutes(row["unauthorized_minutes"]),
                    "Vacaciones" if vacation else row["status"],
                    "Vacaciones autorizadas" if vacation else row["note"],
                ]
            )
        for key, vacation in sorted(
            vacation_days.items(), key=lambda item: (item[0][0], item[0][1])
        ):
            if key in exported_days:
                continue
            writer.writerow(
                [
                    vacation["employee_name"],
                    key[1].isoformat(),
                    "",
                    "",
                    "",
                    "0 h 00 min",
                    "0 h 00 min",
                    "0 h 00 min",
                    "Vacaciones",
                    "Vacaciones autorizadas",
                ]
            )
        filename = f"reporte_horas_extra_{week_start.isoformat()}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @app.get("/expedientes.xlsx")
    @login_required
    def employee_records_xlsx():
        try:
            week_start = week_start_for(
                parse_iso_date(
                    request.args.get("semana", local_today().isoformat())
                )
            )
            group_name = request.args.get("grupo", "").strip()
            active_groups = [
                group
                for group in employee_groups()
                if not is_inactive_group(group)
            ]
            if group_name and group_name not in active_groups:
                abort(400, "El grupo Hikvision seleccionado no es válido.")
            report_rows, _loaded_at, _attendance_dates = report_for_week(
                week_start
            )
        except (ValueError, HikConnectError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("home"))

        rows = build_expediente_rows(
            report_rows,
            employee_directory(),
            week_start,
            local_today(),
            group_name,
            vacations_for_week(week_start),
            attendance_permissions_for_week(week_start),
        )
        workbook = create_expedientes_workbook(
            rows,
            week_start,
            group_name,
        )
        filename = f"expedientes_{week_start.isoformat()}.xlsx"
        return Response(
            workbook,
            mimetype=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @app.get("/expediente-contador.xlsx")
    @login_required
    def accountant_records_xlsx():
        try:
            week_start = week_start_for(
                parse_iso_date(
                    request.args.get("semana", local_today().isoformat())
                )
            )
            group_name = request.args.get("grupo", "").strip()
            active_groups = [
                group
                for group in employee_groups()
                if not is_inactive_group(group)
            ]
            if group_name and group_name not in active_groups:
                abort(400, "El grupo Hikvision seleccionado no es válido.")
            report_rows, _loaded_at, _attendance_dates = report_for_week(
                week_start
            )
        except (ValueError, HikConnectError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("home"))

        rows = build_accountant_rows(
            report_rows,
            employee_directory(),
            week_start,
            group_name,
            vacations_for_week(week_start),
        )
        workbook = create_accountant_workbook(
            rows,
            week_start,
            group_name,
        )
        filename = f"expediente_contador_{week_start.isoformat()}.xlsx"
        return Response(
            workbook,
            mimetype=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    @app.get("/usuarios")
    @login_required
    def users():
        rows = get_db().execute(
            """
            SELECT id, username, display_name, active, supervised_area,
                   must_change_password,
                   users.access_role, users.employee_name_key,
                   users.created_at, employees.area AS worker_area
            FROM users
            LEFT JOIN employees
              ON employees.employee_name_key = users.employee_name_key
            ORDER BY users.display_name
            """
        ).fetchall()
        administrators = [
            row for row in rows if row["access_role"] != "worker"
        ]
        workers_by_area: dict[str, list] = {}
        for row in rows:
            if row["access_role"] != "worker":
                continue
            area = str(row["worker_area"] or "").strip() or "Sin grupo"
            workers_by_area.setdefault(area, []).append(row)
        user_groups = [
            {
                "key": "administrators",
                "title": "Usuarios administradores registrados",
                "rows": administrators,
            }
        ]
        user_groups.extend(
            {
                "key": f"workers-{index}",
                "title": f"Usuarios {area} registrados",
                "rows": workers_by_area[area],
            }
            for index, area in enumerate(
                sorted(workers_by_area, key=str.casefold), start=1
            )
        )
        return render_template(
            "users.html",
            rows=rows,
            user_groups=user_groups,
            employee_areas=[
                group for group in employee_groups()
                if not is_inactive_group(group)
            ],
            employees=employee_directory(),
        )

    @app.post("/usuarios/nuevo")
    @login_required
    def new_user():
        validate_csrf()
        display_name = request.form.get("display_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        supervised_area = request.form.get("supervised_area", "").strip()
        access_role = request.form.get("access_role", "admin").strip()
        employee_name_key = request.form.get(
            "employee_name_key", ""
        ).strip()
        if access_role not in {"admin", "worker"}:
            abort(400, "El tipo de acceso seleccionado no es válido.")
        valid_groups = {
            group for group in employee_groups()
            if not is_inactive_group(group)
        }
        if access_role == "worker":
            employee = get_db().execute(
                """
                SELECT employee_name_key FROM employees
                WHERE employee_name_key = ?
                """,
                (employee_name_key,),
            ).fetchone()
            if employee is None:
                abort(400, "Selecciona el trabajador vinculado a la cuenta.")
            supervised_area = ""
        else:
            employee_name_key = ""
        if supervised_area and supervised_area not in valid_groups:
            abort(400, "El grupo supervisado seleccionado no es válido.")
        if len(display_name) < 2 or len(username) < 3 or len(password) < 10:
            flash(
                "Completa el nombre, usuario y una contraseña de 10 caracteres.",
                "error",
            )
            return redirect(url_for("users"))
        connection = get_db()
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (
                    username, display_name, password_hash,
                    supervised_area, access_role, employee_name_key,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    display_name,
                    generate_password_hash(
                        password, method="pbkdf2:sha256"
                    ),
                    supervised_area,
                    access_role,
                    employee_name_key,
                    utc_now(),
                ),
            )
        except sqlite3.IntegrityError:
            flash("Ese nombre de usuario ya existe.", "error")
            return redirect(url_for("users"))
        log_action(
            g.user["id"],
            "create",
            "user",
            cursor.lastrowid,
            display_name,
        )
        connection.commit()
        flash("Usuario creado correctamente.", "success")
        return redirect(url_for("users"))

    @app.post("/usuarios/<int:user_id>/grupo-supervisado")
    @login_required
    def update_user_supervised_area(user_id: int):
        validate_csrf()
        supervised_area = request.form.get("supervised_area", "").strip()
        valid_groups = {
            group for group in employee_groups()
            if not is_inactive_group(group)
        }
        if supervised_area and supervised_area not in valid_groups:
            abort(400, "El grupo supervisado seleccionado no es válido.")
        connection = get_db()
        user = connection.execute(
            "SELECT id, display_name FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            abort(404)
        connection.execute(
            "UPDATE users SET supervised_area = ? WHERE id = ?",
            (supervised_area, user_id),
        )
        log_action(
            g.user["id"],
            "update_supervised_area",
            "user",
            user_id,
            supervised_area or "Todos los grupos",
        )
        connection.commit()
        flash("Grupo supervisado actualizado.", "success")
        return redirect(url_for("users"))

    @app.post("/usuarios/<int:user_id>/estado")
    @login_required
    def toggle_user(user_id: int):
        validate_csrf()
        if user_id == g.user["id"]:
            flash("No puedes desactivar tu propio acceso.", "error")
            return redirect(url_for("users"))
        connection = get_db()
        user = connection.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            abort(404)
        new_status = 0 if user["active"] else 1
        connection.execute(
            "UPDATE users SET active = ? WHERE id = ?",
            (new_status, user_id),
        )
        log_action(
            g.user["id"],
            "activate" if new_status else "deactivate",
            "user",
            user_id,
            user["display_name"],
        )
        connection.commit()
        flash(
            f"Acceso {'activado' if new_status else 'desactivado'}.",
            "success",
        )
        return redirect(url_for("users"))

    @app.route("/cuenta", methods=("GET", "POST"))
    @login_required
    def account():
        if request.method == "POST":
            validate_csrf()
            username = request.form.get("username", "").strip()
            if g.user["access_role"] == "worker":
                username = username.casefold()
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirmation = request.form.get("password_confirmation", "")
            if not check_password_hash(
                g.user["password_hash"], current_password
            ):
                flash("La contraseña actual no es correcta.", "error")
            elif (
                g.user["access_role"] == "worker"
                and not valid_email(username)
            ):
                flash("Escribe un correo electrónico válido.", "error")
            elif g.user["access_role"] != "worker" and len(username) < 3:
                flash("El usuario debe tener al menos 3 caracteres.", "error")
            elif len(new_password) < 10:
                flash(
                    "La nueva contraseña debe tener al menos 10 caracteres.",
                    "error",
                )
            elif new_password != confirmation:
                flash("Las contraseñas nuevas no coinciden.", "error")
            else:
                connection = get_db()
                duplicate = connection.execute(
                    """
                    SELECT id FROM users
                    WHERE username = ? COLLATE NOCASE AND id <> ?
                    """,
                    (username, g.user["id"]),
                ).fetchone()
                if duplicate is not None:
                    flash("Ese correo o usuario ya pertenece a otra cuenta.", "error")
                    return render_template("account.html")
                connection.execute(
                    """
                    UPDATE users
                    SET username = ?, password_hash = ?,
                        must_change_password = 0
                    WHERE id = ?
                    """,
                    (
                        username,
                        generate_password_hash(
                            new_password, method="pbkdf2:sha256"
                        ),
                        g.user["id"],
                    ),
                )
                log_action(
                    g.user["id"],
                    "change_password",
                    "user",
                    g.user["id"],
                )
                connection.commit()
                flash(
                    "Correo y contraseña actualizados."
                    if g.user["access_role"] == "worker"
                    else "Usuario y contraseña actualizados.",
                    "success",
                )
                return redirect(
                    url_for("home")
                    if g.user["must_change_password"]
                    else url_for("account")
                )
        return render_template("account.html")

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
