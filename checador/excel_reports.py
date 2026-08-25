from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta

import xlsxwriter


WEEKDAY_SHORT = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
DOUBLE_OVERTIME_WEEKLY_LIMIT_MINUTES = 9 * 60


def attendance_incidents(
    work_date: date,
    clock_in: time | None,
    clock_out: time | None,
    day_complete: bool = True,
) -> str:
    if work_date.weekday() == 6:
        return ""
    incidents = []
    if clock_in is None and clock_out is None:
        return "Ausencia a laborar" if day_complete else ""
    if clock_in is None:
        incidents.append("No checó entrada")
    late = (
        clock_in > time(8, 10)
        if clock_in is not None and work_date.weekday() < 5
        else clock_in is not None and clock_in >= time(8, 50)
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
    return " · ".join(incidents)


def build_expediente_rows(
    report_rows: list[dict],
    employees: list[dict],
    week_start: date,
    today: date,
    group_name: str = "",
    vacation_rows: list[dict] | None = None,
    permission_rows: list[dict] | None = None,
) -> list[dict]:
    report_map = {
        (row["employee_name_key"], row["work_date"]): row
        for row in report_rows
    }
    vacation_dates = _vacation_dates(vacation_rows or [], week_start)
    permission_dates = {
        (
            row["employee_name_key"],
            date.fromisoformat(row["work_date"])
            if isinstance(row["work_date"], str)
            else row["work_date"],
        )
        for row in permission_rows or []
    }
    work_days = [
        week_start + timedelta(days=offset)
        for offset in range(7)
        if (week_start + timedelta(days=offset)).weekday() != 6
        and week_start + timedelta(days=offset) <= today
    ]
    selected = [
        employee
        for employee in employees
        if "bajas e inactivos" not in employee["area"].casefold()
        and (not group_name or employee["area"] == group_name)
    ]
    selected.sort(
        key=lambda item: (
            item["employee_code"] or "999999",
            item["employee_name_key"],
        )
    )

    rows = []
    for employee in selected:
        for work_date in work_days:
            report = report_map.get(
                (employee["employee_name_key"], work_date),
                {},
            )
            clock_in = report.get("clock_in")
            clock_out = report.get("clock_out")
            rows.append(
                {
                    "employee_code": employee["employee_code"],
                    "employee_name": employee["employee_name"],
                    "work_date": work_date,
                    "clock_in": clock_in,
                    "clock_out": clock_out,
                    "approved_minutes": int(
                        report.get("approved_minutes", 0)
                    ),
                    "used_minutes": int(
                        report.get("authorized_minutes", 0)
                    ),
                    "notes": (
                        ""
                        if (employee["employee_name_key"], work_date)
                        in permission_dates
                        else (
                            "Vacaciones"
                            if (employee["employee_name_key"], work_date)
                            in vacation_dates
                            else attendance_incidents(
                                work_date,
                                clock_in,
                                clock_out,
                                day_complete=work_date < today,
                            )
                        )
                    ),
                }
            )
    return rows


def round_overtime_code_hours(minutes: int) -> int:
    """Round overtime to closed hours, only raising remainders of 50+ min."""
    safe_minutes = max(0, int(minutes))
    hours, remainder = divmod(safe_minutes, 60)
    return hours + (1 if remainder >= 50 else 0)


def _vacation_dates(vacation_rows: list[dict], week_start: date) -> set[tuple]:
    dates = set()
    week_end = week_start + timedelta(days=6)
    for vacation in vacation_rows:
        start = vacation["start_date"]
        end = vacation["end_date"]
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if isinstance(end, str):
            end = date.fromisoformat(end)
        current = max(start, week_start)
        final = min(end, week_end)
        while current <= final:
            if current.weekday() != 6:
                dates.add((vacation["employee_name_key"], current))
            current += timedelta(days=1)
    return dates


def build_accountant_rows(
    report_rows: list[dict],
    employees: list[dict],
    week_start: date,
    group_name: str = "",
    vacation_rows: list[dict] | None = None,
) -> list[dict]:
    report_map = {
        (row["employee_name_key"], row["work_date"]): row
        for row in report_rows
    }
    vacation_dates = _vacation_dates(vacation_rows or [], week_start)
    week_days = [week_start + timedelta(days=offset) for offset in range(7)]
    selected = [
        employee
        for employee in employees
        if "bajas e inactivos" not in employee["area"].casefold()
        and (not group_name or employee["area"] == group_name)
    ]
    selected.sort(
        key=lambda item: (
            item["employee_code"] or "999999",
            item["employee_name_key"],
        )
    )

    rows = []
    for employee in selected:
        weekly_overtime_used = 0
        day_codes = []
        for work_date in week_days:
            if (employee["employee_name_key"], work_date) in vacation_dates:
                day_codes.append("VACACIONES")
                continue
            report = report_map.get(
                (employee["employee_name_key"], work_date),
                {},
            )
            used_minutes = max(0, int(report.get("authorized_minutes", 0)))
            double_minutes = min(
                used_minutes,
                max(
                    0,
                    DOUBLE_OVERTIME_WEEKLY_LIMIT_MINUTES
                    - weekly_overtime_used,
                ),
            )
            triple_minutes = max(0, used_minutes - double_minutes)
            weekly_overtime_used += used_minutes

            codes = []
            double_hours = round_overtime_code_hours(double_minutes)
            triple_hours = round_overtime_code_hours(triple_minutes)
            if double_hours:
                codes.append(f"{double_hours}HE2")
            if triple_hours:
                codes.append(f"{triple_hours}HE3")
            day_codes.append(" / ".join(codes))

        rows.append(
            {
                "employee_code": employee["employee_code"],
                "employee_name": employee["employee_name"],
                "day_codes": day_codes,
            }
        )
    return rows


def create_expedientes_workbook(
    rows: list[dict],
    week_start: date,
    group_name: str = "",
) -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    workbook.set_properties(
        {
            "title": "Expedientes de asistencia",
            "subject": "Entradas, salidas, horas extra e incidencias",
            "company": "TecnoAll",
        }
    )
    sheet = workbook.add_worksheet("Expedientes")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(4, 0)

    title_format = workbook.add_format(
        {
            "bold": True,
            "font_size": 16,
            "font_color": "#FFFFFF",
            "bg_color": "#006892",
            "align": "left",
            "valign": "vcenter",
        }
    )
    label_format = workbook.add_format(
        {"bold": True, "font_color": "#405766"}
    )
    metadata_format = workbook.add_format(
        {"font_color": "#1D2D3D"}
    )
    code_format = workbook.add_format(
        {"num_format": "000", "align": "center"}
    )
    date_format = workbook.add_format(
        {"num_format": "dd/mm/yyyy", "align": "center"}
    )
    time_format = workbook.add_format(
        {"num_format": "hh:mm", "align": "center"}
    )
    duration_format = workbook.add_format(
        {"num_format": "[h]:mm", "align": "center"}
    )
    incident_format = workbook.add_format(
        {"font_color": "#C62828", "bold": True, "text_wrap": True}
    )

    sheet.set_row(0, 30)
    sheet.merge_range("A1:H1", "Expedientes de asistencia", title_format)
    sheet.write("A2", "Periodo", label_format)
    sheet.write(
        "B2",
        (
            f"{week_start.strftime('%d/%m/%Y')} — "
            f"{(week_start + timedelta(days=6)).strftime('%d/%m/%Y')}"
        ),
        metadata_format,
    )
    sheet.write("E2", "Grupo Hikvision", label_format)
    sheet.merge_range(
        "F2:H2",
        group_name or "Todos los grupos activos",
        metadata_format,
    )

    headers = [
        "No. Empleado",
        "Nombre del Trabajador",
        "Fecha",
        "Hora Entrada",
        "Hora Salida",
        "Horas Extras Aprobadas",
        "Horas Extras Utilizadas",
        "Notas",
    ]
    data = [
        [
            (
                int(row["employee_code"])
                if row["employee_code"].isdigit()
                else row["employee_code"]
            ),
            row["employee_name"],
            datetime.combine(row["work_date"], time.min),
            (
                datetime.combine(row["work_date"], row["clock_in"])
                if row["clock_in"]
                else ""
            ),
            (
                datetime.combine(row["work_date"], row["clock_out"])
                if row["clock_out"]
                else ""
            ),
            row["approved_minutes"] / (24 * 60),
            row["used_minutes"] / (24 * 60),
            row["notes"],
        ]
        for row in rows
    ]

    table_end_row = max(4, 3 + len(data))
    if data:
        sheet.add_table(
            3,
            0,
            table_end_row,
            7,
            {
                "name": "ExpedientesTable",
                "style": "Table Style Medium 2",
                "columns": [{"header": header} for header in headers],
                "data": data,
            },
        )
        first_data_row = 4
        last_data_row = table_end_row
        sheet.set_column("A:A", 14, code_format)
        sheet.set_column("B:B", 33)
        sheet.set_column("C:C", 13, date_format)
        sheet.set_column("D:E", 14, time_format)
        sheet.set_column("F:G", 23, duration_format)
        sheet.set_column("H:H", 34)
        for row_index, row in enumerate(rows, start=first_data_row):
            if row["notes"]:
                sheet.write(row_index, 7, row["notes"], incident_format)
        sheet.set_default_row(20)
        sheet.set_row(last_data_row, 20)
    else:
        header_format = workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#006892",
            }
        )
        sheet.write_row(3, 0, headers, header_format)
        sheet.autofilter(3, 0, 3, 7)
        sheet.set_column("A:A", 14, code_format)
        sheet.set_column("B:B", 33)
        sheet.set_column("C:E", 14)
        sheet.set_column("F:G", 23)
        sheet.set_column("H:H", 34)

    sheet.set_landscape()
    sheet.fit_to_pages(1, 0)
    sheet.repeat_rows(3)
    sheet.set_margins(0.3, 0.3, 0.5, 0.5)
    workbook.close()
    return output.getvalue()


def create_accountant_workbook(
    rows: list[dict],
    week_start: date,
    group_name: str = "",
) -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    workbook.set_properties(
        {
            "title": "Expediente Contador",
            "subject": "Horas extra dobles y triples por semana",
            "company": "TecnoAll",
        }
    )
    sheet = workbook.add_worksheet("Expediente Contador")
    sheet.hide_gridlines(2)
    sheet.freeze_panes(4, 2)

    title_format = workbook.add_format(
        {
            "bold": True,
            "font_size": 16,
            "font_color": "#FFFFFF",
            "bg_color": "#006892",
            "align": "left",
            "valign": "vcenter",
        }
    )
    label_format = workbook.add_format(
        {"bold": True, "font_color": "#405766"}
    )
    metadata_format = workbook.add_format({"font_color": "#1D2D3D"})
    code_format = workbook.add_format(
        {"num_format": "000", "align": "center"}
    )
    overtime_format = workbook.add_format(
        {"bold": True, "font_color": "#005B85", "align": "center"}
    )

    sheet.set_row(0, 30)
    sheet.merge_range("A1:I1", "Expediente Contador", title_format)
    sheet.write("A2", "Periodo", label_format)
    sheet.write(
        "B2",
        (
            f"{week_start.strftime('%d/%m/%Y')} — "
            f"{(week_start + timedelta(days=6)).strftime('%d/%m/%Y')}"
        ),
        metadata_format,
    )
    sheet.write("F2", "Grupo Hikvision", label_format)
    sheet.merge_range(
        "G2:I2",
        group_name or "Todos los grupos activos",
        metadata_format,
    )

    week_days = [week_start + timedelta(days=offset) for offset in range(7)]
    headers = ["No. Empleado", "Nombre del Trabajador"] + [
        f"{WEEKDAY_SHORT[work_date.weekday()]} {work_date.strftime('%d/%m')}"
        for work_date in week_days
    ]
    data = [
        [
            (
                int(row["employee_code"])
                if row["employee_code"].isdigit()
                else row["employee_code"]
            ),
            row["employee_name"],
            *row["day_codes"],
        ]
        for row in rows
    ]

    table_end_row = max(4, 3 + len(data))
    if data:
        sheet.add_table(
            3,
            0,
            table_end_row,
            8,
            {
                "name": "ExpedienteContadorTable",
                "style": "Table Style Medium 2",
                "columns": [{"header": header} for header in headers],
                "data": data,
            },
        )
        for row_index, row in enumerate(rows, start=4):
            for column_index, value in enumerate(row["day_codes"], start=2):
                if value:
                    sheet.write(
                        row_index,
                        column_index,
                        value,
                        overtime_format,
                    )
    else:
        header_format = workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#006892",
            }
        )
        sheet.write_row(3, 0, headers, header_format)
        sheet.autofilter(3, 0, 3, 8)

    sheet.set_column("A:A", 14, code_format)
    sheet.set_column("B:B", 34)
    sheet.set_column("C:I", 14)
    sheet.set_default_row(20)
    sheet.set_landscape()
    sheet.fit_to_pages(1, 0)
    sheet.repeat_rows(3)
    sheet.set_margins(0.3, 0.3, 0.5, 0.5)
    workbook.close()
    return output.getvalue()
