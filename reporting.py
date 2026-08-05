from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta


def normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.strip())
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return " ".join(without_accents.casefold().split())


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    cleaned = value.strip().replace("/", "-")
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        return None


def parse_clock(value: str | None) -> time | None:
    if not value:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour=hour, minute=minute)


def duration_minutes(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    match = re.search(r"(\d+):(\d{2})", str(value).strip())
    if not match:
        return 0
    return int(match.group(1)) * 60 + int(match.group(2))


def format_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{minutes // 60} h {minutes % 60:02d} min"


def scheduled_overtime_intervals(
    work_date: date,
    clock_in: time | None,
    clock_out: time | None,
) -> list[tuple[datetime, datetime]]:
    if clock_in is None or clock_out is None:
        return []

    day_start = datetime.combine(work_date, time.min)
    start = datetime.combine(work_date, clock_in)
    end = datetime.combine(work_date, clock_out)
    if end < start:
        end += timedelta(days=1)

    if work_date.weekday() <= 4:
        schedule_start = day_start.replace(hour=8)
        schedule_end = day_start.replace(hour=17)
    elif work_date.weekday() == 5:
        schedule_start = day_start.replace(hour=8, minute=30)
        schedule_end = day_start.replace(hour=13)
    else:
        return [(start, end)] if end > start else []

    intervals = []
    early_end = min(end, schedule_start)
    if start < early_end:
        intervals.append((start, early_end))

    late_start = max(start, schedule_end)
    if late_start < end:
        intervals.append((late_start, end))
    return intervals


def scheduled_overtime_minutes(
    work_date: date,
    clock_in: time | None,
    clock_out: time | None,
) -> int:
    return sum(
        int((end - start).total_seconds() // 60)
        for start, end in scheduled_overtime_intervals(
            work_date,
            clock_in,
            clock_out,
        )
    )


def build_daily_attendance(reports: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, date], dict] = {}

    for source in reports:
        name = str(source.get("fullName") or "").strip()
        if not name:
            name = " ".join(
                part
                for part in (
                    str(source.get("firstName") or "").strip(),
                    str(source.get("lastName") or "").strip(),
                )
                if part
            )
        work_date = (
            parse_date(source.get("clockOutDate"))
            or parse_date(source.get("clockInDate"))
        )
        if not name or not work_date:
            continue

        key = (normalize_name(name), work_date)
        row = grouped.setdefault(
            key,
            {
                "employee_name": name,
                "employee_name_key": key[0],
                "group_name": str(source.get("groupName") or "").strip(),
                "work_date": work_date,
                "clock_in": None,
                "clock_out": None,
                "overtime_minutes": 0,
                "area": "",
            },
        )
        clock_in = parse_clock(source.get("clockInTime"))
        clock_out = parse_clock(source.get("clockOutTime"))
        if clock_in and (row["clock_in"] is None or clock_in < row["clock_in"]):
            row["clock_in"] = clock_in
        if clock_out and (
            row["clock_out"] is None or clock_out > row["clock_out"]
        ):
            row["clock_out"] = clock_out
        row["area"] = (
            str(source.get("clockOutArea") or "").strip()
            or str(source.get("clockInArea") or "").strip()
            or row["area"]
        )
        group_name = str(source.get("groupName") or "").strip()
        if group_name:
            row["group_name"] = group_name

    rows = list(grouped.values())
    for row in rows:
        row["overtime_intervals"] = scheduled_overtime_intervals(
            row["work_date"],
            row["clock_in"],
            row["clock_out"],
        )
        row["overtime_minutes"] = sum(
            int((end - start).total_seconds() // 60)
            for start, end in row["overtime_intervals"]
        )

    return sorted(
        rows,
        key=lambda item: (item["work_date"], item["employee_name_key"]),
    )


def compare_overtime(
    attendance: dict | None,
    authorization: dict | list[dict] | None,
) -> dict:
    authorizations = (
        authorization
        if isinstance(authorization, list)
        else ([authorization] if authorization else [])
    )
    first_authorization = authorizations[0] if authorizations else None
    employee_name = (
        (attendance or {}).get("employee_name")
        or (first_authorization or {}).get("employee_name")
        or ""
    )
    work_date = (
        (attendance or {}).get("work_date")
        or date.fromisoformat(first_authorization["work_date"])
    )
    overtime_minutes = int((attendance or {}).get("overtime_minutes", 0))
    actual_intervals = list(
        (attendance or {}).get("overtime_intervals") or []
    )
    allowed_minutes = 0
    authorized_minutes = 0

    if (
        not actual_intervals
        and attendance
        and attendance.get("clock_out")
        and overtime_minutes
    ):
        fallback_end = datetime.combine(
            work_date, attendance["clock_out"]
        )
        actual_intervals = [
            (
                fallback_end - timedelta(minutes=overtime_minutes),
                fallback_end,
            )
        ]

    allowed_intervals = []
    authorization_limits = []
    for item in authorizations:
        start_time = parse_clock(item["allowed_start"])
        end_time = parse_clock(item["allowed_end"])
        if start_time and end_time:
            allowed_start = datetime.combine(work_date, start_time)
            allowed_end = datetime.combine(work_date, end_time)
            if allowed_end <= allowed_start:
                allowed_end += timedelta(days=1)
            allowed_intervals.append((allowed_start, allowed_end))
            interval_minutes = int(
                (allowed_end - allowed_start).total_seconds() // 60
            )
            approved_minutes = item.get("approved_minutes")
            authorization_limits.append(
                min(
                    interval_minutes,
                    max(0, int(approved_minutes)),
                )
                if approved_minutes is not None
                else interval_minutes
            )

    def merge_intervals(
        intervals: list[tuple[datetime, datetime]],
    ) -> list[tuple[datetime, datetime]]:
        merged: list[list[datetime]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(start, end) for start, end in merged]

    individual_allowed_intervals = allowed_intervals
    allowed_intervals = merge_intervals(individual_allowed_intervals)
    allowed_minutes = sum(authorization_limits)

    authorized_intervals = []
    authorized_by_authorization = 0
    for (allowed_start, allowed_end), limit in zip(
        individual_allowed_intervals,
        authorization_limits,
    ):
        item_intervals = []
        for actual_start, actual_end in actual_intervals:
            overlap_start = max(actual_start, allowed_start)
            overlap_end = min(actual_end, allowed_end)
            if overlap_end > overlap_start:
                item_intervals.append((overlap_start, overlap_end))
                authorized_intervals.append((overlap_start, overlap_end))
        item_overlap_minutes = sum(
            int((end - start).total_seconds() // 60)
            for start, end in merge_intervals(item_intervals)
        )
        authorized_by_authorization += min(item_overlap_minutes, limit)
    authorized_overlap_minutes = sum(
        int((end - start).total_seconds() // 60)
        for start, end in merge_intervals(authorized_intervals)
    )
    authorized_minutes = min(
        authorized_overlap_minutes,
        authorized_by_authorization,
    )

    unauthorized_minutes = max(0, overtime_minutes - authorized_minutes)
    unused_minutes = max(0, allowed_minutes - authorized_minutes)

    if overtime_minutes == 0 and authorizations:
        status = "Autorización no utilizada"
        status_key = "unused"
    elif overtime_minutes > 0 and not authorizations:
        status = "Horas no autorizadas"
        status_key = "unauthorized"
    elif unauthorized_minutes > 0:
        status = "Excedió autorización"
        status_key = "exceeded"
    elif authorized_minutes > 0:
        status = "Dentro de autorización"
        status_key = "authorized"
    else:
        status = "Sin horas extra"
        status_key = "normal"

    actual_range = ", ".join(
        f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        for start, end in actual_intervals
    ) or "—"

    return {
        "employee_name": employee_name,
        "employee_name_key": normalize_name(employee_name),
        "work_date": work_date,
        "clock_in": (attendance or {}).get("clock_in"),
        "clock_out": (attendance or {}).get("clock_out"),
        "area": (attendance or {}).get("area", ""),
        "actual_range": actual_range,
        "allowed_range": (
            ", ".join(
                f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
                for start, end in allowed_intervals
            )
            if allowed_intervals
            else "Sin autorización"
        ),
        "overtime_minutes": overtime_minutes,
        "authorized_minutes": authorized_minutes,
        "approved_minutes": allowed_minutes,
        "unauthorized_minutes": unauthorized_minutes,
        "unused_minutes": unused_minutes,
        "status": status,
        "status_key": status_key,
        "note": " · ".join(
            dict.fromkeys(
                item.get("note", "")
                for item in authorizations
                if item.get("note", "")
            )
        ),
    }


def build_weekly_report(
    attendance_rows: list[dict],
    authorizations: list[dict],
) -> list[dict]:
    attendance_map = {
        (row["employee_name_key"], row["work_date"]): row
        for row in attendance_rows
    }
    authorization_map: dict[tuple[str, date], list[dict]] = {}
    for row in authorizations:
        authorization_map.setdefault(
            (
                row["employee_name_key"],
                date.fromisoformat(row["work_date"]),
            ),
            [],
        ).append(row)
    keys = set(attendance_map) | set(authorization_map)
    result = []
    for key in sorted(keys, key=lambda item: (item[1], item[0])):
        attendance = attendance_map.get(key)
        authorization = authorization_map.get(key)
        result.append(compare_overtime(attendance, authorization))
    return result
