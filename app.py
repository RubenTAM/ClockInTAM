from __future__ import annotations

import csv
import io
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
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
from excel_reports import (
    build_accountant_rows,
    build_expediente_rows,
    create_accountant_workbook,
    create_expedientes_workbook,
    round_overtime_code_hours,
)
from hikconnect import HikConnectClient, HikConnectError
from reporting import (
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


def known_employee_code(employee_name: str) -> str:
    return EMPLOYEE_CODES_BY_NAME.get(
        employee_identity_key(employee_name),
        "",
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
        SELECT employee_name, employee_name_key, employee_code, area
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


def rounded_overtime_minutes(minutes: int) -> int:
    """Apply the company 50-minute threshold and return whole-hour minutes."""
    return round_overtime_code_hours(minutes) * 60


def supervisor_incident_summary(calendar_rows: list[dict]) -> dict:
    """Build the supervisor preview from missing punches and approved overtime."""
    totals = {
        "workers": len(calendar_rows),
        "with_incidents": 0,
        "incomplete": 0,
        "authorized_minutes": 0,
        "double_minutes": 0,
        "triple_minutes": 0,
    }
    for worker in calendar_rows:
        incidents = []
        worker_vacation_days = 0
        worker_authorized = 0
        worker_double = 0
        worker_triple = 0
        worker_incomplete = 0
        for cell in worker["cells"]:
            report = cell["report"]
            is_vacation = bool(cell.get("is_vacation"))
            if is_vacation:
                worker_vacation_days += 1
            missing_punches = []
            if cell["is_incomplete"] and not cell["is_today"]:
                if not report or not report.get("clock_in"):
                    worker_incomplete += 1
                    missing_punches.append("Sin checada de entrada")
                if not report or not report.get("clock_out"):
                    worker_incomplete += 1
                    missing_punches.append("Sin checada de salida")
            authorized = 0
            double_minutes = 0
            triple_minutes = 0
            if report:
                authorized = rounded_overtime_minutes(
                    int(report.get("approved_minutes", 0))
                )
                double_minutes = int(
                    report.get("rounded_double_minutes", 0)
                )
                triple_minutes = int(
                    report.get("rounded_triple_minutes", 0)
                )
                worker_authorized += authorized
                worker_double += double_minutes
                worker_triple += triple_minutes
            if is_vacation or missing_punches or authorized:
                incidents.append(
                    {
                        "work_date": cell["work_date"],
                        "is_vacation": is_vacation,
                        "missing_punches": missing_punches,
                        "authorized_minutes": authorized,
                        "double_minutes": double_minutes,
                        "triple_minutes": triple_minutes,
                    }
                )
        worker["summary_incidents"] = incidents
        worker["summary_vacation_days"] = worker_vacation_days
        worker["summary_incomplete"] = worker_incomplete
        worker["summary_authorized_minutes"] = worker_authorized
        worker["summary_double_minutes"] = worker_double
        worker["summary_triple_minutes"] = worker_triple
        if incidents:
            totals["with_incidents"] += 1
        totals["incomplete"] += worker_incomplete
        totals["authorized_minutes"] += worker_authorized
        totals["double_minutes"] += worker_double
        totals["triple_minutes"] += worker_triple
    return totals


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
            if week_start <= vacation_date <= week_start + timedelta(days=6):
                vacation_map[(vacation["employee_name_key"], vacation_date)] = (
                    vacation
                )

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
            is_non_working = (
                work_date.weekday() == 6
                and report is None
                and vacation is None
            )
            is_incomplete = (
                not is_future
                and not is_non_working
                and vacation is None
                and (
                    report is None
                    or not report.get("clock_in")
                    or not report.get("clock_out")
                )
            )
            cells.append(
                {
                    "work_date": work_date,
                    "report": report,
                    "is_today": is_today,
                    "is_future": is_future,
                    "is_non_working": is_non_working,
                    "is_incomplete": is_incomplete,
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
            )
        except HikConnectError as exc:
            error = str(exc)
            calendar_rows, week_days = weekly_report_calendar(
                [], week_start, vacations_for_week(week_start)
            )
        directory = {
            row["employee_name_key"]: row
            for row in employee_directory()
        }
        for worker in calendar_rows:
            employee = directory.get(worker["employee_name_key"], {})
            worker["area"] = employee.get("area", "")
            worker["employee_code"] = employee.get("employee_code", "")
        calendar_rows = [
            worker
            for worker in calendar_rows
            if not is_inactive_group(worker["area"])
        ]
        supervisor_area = str(g.user["supervised_area"] or "").strip()
        if supervisor_area:
            calendar_rows = [
                worker
                for worker in calendar_rows
                if same_group(worker["area"], supervisor_area)
            ]
        supervisor_totals = supervisor_incident_summary(calendar_rows)
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
            selected_worker=request.args.get("trabajador", ""),
            supervisor_area=supervisor_area,
            supervisor_totals=supervisor_totals,
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
        vacation_rows = get_db().execute(
            """
            SELECT v.*, e.area, u.display_name AS created_by_name
            FROM vacations v
            LEFT JOIN employees e
              ON e.employee_name_key = v.employee_name_key
            JOIN users u ON u.id = v.created_by
            WHERE (? = '' OR e.area = ? COLLATE NOCASE)
            ORDER BY v.start_date DESC, v.employee_name_key
            """,
            (selected_group, selected_group),
        ).fetchall()
        return render_template(
            "vacations.html",
            employees=workers,
            employee_areas=available_groups,
            selected_group=selected_group,
            vacation_rows=vacation_rows,
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
                vacation_days[(vacation["employee_name_key"], current)] = vacation
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
                   created_at
            FROM users ORDER BY display_name
            """
        ).fetchall()
        return render_template(
            "users.html",
            rows=rows,
            employee_areas=[
                group for group in employee_groups()
                if not is_inactive_group(group)
            ],
        )

    @app.post("/usuarios/nuevo")
    @login_required
    def new_user():
        validate_csrf()
        display_name = request.form.get("display_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        supervised_area = request.form.get("supervised_area", "").strip()
        valid_groups = {
            group for group in employee_groups()
            if not is_inactive_group(group)
        }
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
                    supervised_area, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    username,
                    display_name,
                    generate_password_hash(
                        password, method="pbkdf2:sha256"
                    ),
                    supervised_area or default_supervised_area(display_name),
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
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirmation = request.form.get("password_confirmation", "")
            if not check_password_hash(
                g.user["password_hash"], current_password
            ):
                flash("La contraseña actual no es correcta.", "error")
            elif len(new_password) < 10:
                flash(
                    "La nueva contraseña debe tener al menos 10 caracteres.",
                    "error",
                )
            elif new_password != confirmation:
                flash("Las contraseñas nuevas no coinciden.", "error")
            else:
                connection = get_db()
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (
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
                flash("Contraseña actualizada.", "success")
                return redirect(url_for("account"))
        return render_template("account.html")

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
