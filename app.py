from __future__ import annotations

import csv
import io
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from time import monotonic

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
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

import database
from database import get_db, log_action, utc_now
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
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    database.init_app(app)

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
            "today": date.today(),
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


def monday_for(value: date) -> date:
    return value - timedelta(days=value.weekday())


def cached_attendance(work_date: date, force: bool = False) -> list[dict]:
    cache: dict = current_app.extensions["attendance_cache"]
    key = work_date.isoformat()
    cached = cache.get(key)
    if cached and not force and monotonic() - cached["saved_at"] < 300:
        return cached["rows"]

    client: HikConnectClient = current_app.extensions["hik_client"]
    reports = client.attendance_for_date(work_date)
    rows = build_daily_attendance(reports)
    cache[key] = {"saved_at": monotonic(), "rows": rows}
    return rows


def authorizations_for_week(week_start: date) -> list[dict]:
    week_end = week_start + timedelta(days=6)
    rows = get_db().execute(
        """
        SELECT a.*, u.display_name AS created_by_name
        FROM overtime_authorizations a
        JOIN users u ON u.id = a.created_by
        WHERE a.work_date BETWEEN ? AND ?
        ORDER BY a.work_date, a.employee_name_key
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def report_for_week(
    week_start: date,
    force: bool = False,
) -> tuple[list[dict], datetime]:
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
    return rows, datetime.now()


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
                        username, display_name, password_hash, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        username,
                        display_name,
                        generate_password_hash(
                            password, method="pbkdf2:sha256"
                        ),
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
            return redirect(url_for("dashboard"))

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
                    next_url = url_for("dashboard")
                return redirect(next_url)
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        week_start = monday_for(date.today())
        week_end = week_start + timedelta(days=6)
        connection = get_db()
        stats = connection.execute(
            """
            WITH authorization_minutes AS (
                SELECT
                    employee_name_key,
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
            (date.today().isoformat(),),
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
        requested = request.args.get("semana", date.today().isoformat())
        try:
            week_start = monday_for(parse_iso_date(requested, "semana"))
        except ValueError:
            week_start = monday_for(date.today())
        return render_template(
            "authorizations.html",
            rows=authorizations_for_week(week_start),
            week_start=week_start,
            week_end=week_start + timedelta(days=6),
            previous_week=week_start - timedelta(days=7),
            next_week=week_start + timedelta(days=7),
        )

    @app.route("/autorizaciones/nueva", methods=("GET", "POST"))
    @login_required
    def new_authorization():
        default_week = monday_for(date.today())
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
                "reference_date", date.today().isoformat()
            )

            errors = []
            if not employee_names:
                errors.append("Selecciona al menos un trabajador.")
            if not work_dates:
                errors.append("Selecciona al menos un día.")
            if not allowed_start or not allowed_end:
                errors.append("Indica el horario autorizado.")

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
                        existing = connection.execute(
                            """
                            SELECT id FROM overtime_authorizations
                            WHERE employee_name_key = ? AND work_date = ?
                            """,
                            (name_key, work_date.isoformat()),
                        ).fetchone()
                        if existing:
                            connection.execute(
                                """
                                UPDATE overtime_authorizations
                                SET employee_name = ?, allowed_start = ?,
                                    allowed_end = ?, note = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    employee_name,
                                    allowed_start,
                                    allowed_end,
                                    note,
                                    now,
                                    existing["id"],
                                ),
                            )
                            log_action(
                                g.user["id"],
                                "update",
                                "authorization",
                                existing["id"],
                                json.dumps(
                                    {
                                        "employee": employee_name,
                                        "date": work_date.isoformat(),
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        else:
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
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        affected += 1
                connection.commit()
                flash(
                    f"Se guardaron {affected} autorizaciones.",
                    "success",
                )
                return redirect(
                    url_for(
                        "authorizations",
                        semana=min(parsed_dates).isoformat(),
                    )
                )

            try:
                default_week = monday_for(parse_iso_date(work_dates[0]))
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
            reference_date=date.today().isoformat(),
            submitted=None,
        )

    @app.post("/autorizaciones/<int:authorization_id>/eliminar")
    @login_required
    def delete_authorization(authorization_id: int):
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
                request.args.get("fecha", date.today().isoformat())
            )
            rows = cached_attendance(
                reference_date,
                force=request.args.get("actualizar") == "1",
            )
            workers = sorted(
                (
                    {
                        "name": row["employee_name"],
                        "area": row["area"],
                    }
                    for row in rows
                ),
                key=lambda row: normalize_name(row["name"]),
            )
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
        requested = request.args.get("semana", date.today().isoformat())
        try:
            week_start = monday_for(parse_iso_date(requested, "semana"))
        except ValueError:
            week_start = monday_for(date.today())

        rows = []
        loaded_at = None
        error = None
        try:
            rows, loaded_at = report_for_week(
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
        return render_template(
            "report.html",
            rows=rows,
            totals=totals,
            error=error,
            loaded_at=loaded_at,
            week_start=week_start,
            week_end=week_start + timedelta(days=6),
            previous_week=week_start - timedelta(days=7),
            next_week=week_start + timedelta(days=7),
        )

    @app.get("/reporte.csv")
    @login_required
    def report_csv():
        try:
            week_start = monday_for(
                parse_iso_date(
                    request.args.get("semana", date.today().isoformat())
                )
            )
            rows, _loaded_at = report_for_week(week_start)
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
                "Horario extra real",
                "Horario autorizado",
                "Horas extra detectadas",
                "Horas autorizadas",
                "Horas no autorizadas",
                "Estado",
                "Comentario",
            ]
        )
        for row in rows:
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
                    row["actual_range"],
                    row["allowed_range"],
                    format_minutes(row["overtime_minutes"]),
                    format_minutes(row["authorized_minutes"]),
                    format_minutes(row["unauthorized_minutes"]),
                    row["status"],
                    row["note"],
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

    @app.get("/usuarios")
    @login_required
    def users():
        rows = get_db().execute(
            """
            SELECT id, username, display_name, active, created_at
            FROM users ORDER BY display_name
            """
        ).fetchall()
        return render_template("users.html", rows=rows)

    @app.post("/usuarios/nuevo")
    @login_required
    def new_user():
        validate_csrf()
        display_name = request.form.get("display_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
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
                    username, display_name, password_hash, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    username,
                    display_name,
                    generate_password_hash(
                        password, method="pbkdf2:sha256"
                    ),
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
