from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import requests


class HikConnectError(RuntimeError):
    pass


def _post_json(
    url: str,
    payload: dict,
    timeout: int,
    token: str | None = None,
) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=utf-8",
    }
    if token:
        headers["Token"] = token

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise HikConnectError(
            "No se pudo conectar con Hik-Connect."
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise HikConnectError(
            f"Hik-Connect respondió HTTP {response.status_code} sin JSON."
        ) from exc

    if response.status_code >= 400:
        raise HikConnectError(
            f"Hik-Connect respondió HTTP {response.status_code}."
        )

    if str(data.get("errorCode", "")) != "0":
        message = str(data.get("message", "")).strip()
        raise HikConnectError(
            message or "Hik-Connect rechazó la solicitud."
        )

    return data


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _offset_for(day: date, timezone_name: str) -> str:
    zone = ZoneInfo(timezone_name)
    instant = datetime.combine(day, time(hour=12), tzinfo=zone)
    offset = instant.strftime("%z")
    return f"{offset[:3]}:{offset[3:]}"


class HikConnectClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        timezone_name: str = "America/Tijuana",
        timeout: int = 35,
        page_size: int = 200,
    ):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.initial_base_url = base_url.rstrip("/")
        self.timezone_name = timezone_name
        self.timeout = timeout
        self.page_size = page_size
        self._token = ""
        self._regional_url = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def authenticate(self) -> None:
        if not self.configured:
            raise HikConnectError(
                "Faltan las credenciales de Hik-Connect."
            )

        data = _post_json(
            f"{self.initial_base_url}/api/hccgw/platform/v1/token/get",
            {"appKey": self.api_key, "secretKey": self.api_secret},
            self.timeout,
        ).get("data", {})

        self._token = _clean(data.get("accessToken"))
        self._regional_url = _clean(
            data.get("areaDomain", self.initial_base_url)
        ).rstrip("/")

        if not self._token:
            raise HikConnectError(
                "Hik-Connect no devolvió un token de acceso."
            )

    def attendance_for_date(self, work_date: date) -> list[dict]:
        if not self._token:
            self.authenticate()

        offset = _offset_for(work_date, self.timezone_name)
        date_text = work_date.isoformat()
        begin = f"{date_text}T00:00:00{offset}"
        end = f"{date_text}T23:59:59{offset}"
        url = (
            f"{self._regional_url}"
            "/api/hccgw/attendance/v1/report/totaltimecard/list"
        )

        page = 1
        reports: list[dict] = []
        while True:
            payload = {
                "pageIndex": page,
                "pageSize": self.page_size,
                "beginTime": begin,
                "endTime": end,
                "personName": "",
                "personCode": "",
                "personGroupIds": [],
            }
            try:
                response = _post_json(
                    url,
                    payload,
                    self.timeout,
                    token=self._token,
                )
            except HikConnectError:
                self.authenticate()
                response = _post_json(
                    url,
                    payload,
                    self.timeout,
                    token=self._token,
                )
            data = response.get("data", {})
            rows = data.get("reportDataList", [])
            if not isinstance(rows, list):
                rows = []
            reports.extend(rows)

            more_data = str(data.get("moreData", 0)).lower()
            if not rows or more_data not in {"1", "true"}:
                break
            page += 1

        return reports
