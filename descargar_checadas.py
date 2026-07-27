from __future__ import annotations
import csv
import os
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv


load_dotenv()

API_KEY = os.getenv("HIK_API_KEY", "").strip()
API_SECRET = os.getenv("HIK_API_SECRET", "").strip()
INITIAL_BASE_URL = os.getenv(
    "HIK_BASE_URL",
    "https://ius.hikcentralconnect.com",
).rstrip("/")

TIMEOUT = 35
PAGE_SIZE = 200


def validar_configuracion() -> None:
    if not API_KEY or not API_SECRET:
        print(
            "Faltan HIK_API_KEY o HIK_API_SECRET "
            "en el archivo .env."
        )
        sys.exit(1)


def post_json(
    url: str,
    payload: dict,
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
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"No se pudo conectar con Hik-Connect:\n{exc}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Hik-Connect respondió HTTP {response.status_code}, "
            "pero no devolvió JSON:\n"
            f"{response.text[:1000]}"
        ) from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code}:\n{data}"
        )

    error_code = str(data.get("errorCode", ""))

    if error_code != "0":
        raise RuntimeError(
            "Hik-Connect rechazó la solicitud.\n"
            f"errorCode: {error_code}\n"
            f"message: {data.get('message', '')}\n"
            f"respuesta: {data}"
        )

    return data


def obtener_token() -> tuple[str, str]:
    url = (
        f"{INITIAL_BASE_URL}"
        "/api/hccgw/platform/v1/token/get"
    )

    payload = {
        "appKey": API_KEY,
        "secretKey": API_SECRET,
    }

    print("Conectando con Hik-Connect...")

    respuesta = post_json(url, payload)
    data = respuesta.get("data", {})

    token = str(data.get("accessToken", "")).strip()
    area_domain = str(
        data.get("areaDomain", INITIAL_BASE_URL)
    ).strip().rstrip("/")

    if not token:
        raise RuntimeError(
            "Hik-Connect no devolvió accessToken."
        )

    print("Autenticación correcta.")
    print(f"Servidor regional: {area_domain}")

    return token, area_domain


def pedir_fecha() -> str:
    fecha = input(
        "Fecha a consultar [AAAA-MM-DD]: "
    ).strip()

    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise RuntimeError(
            "La fecha debe escribirse como AAAA-MM-DD, "
            "por ejemplo 2026-07-22."
        )

    return fecha


def normalizar_texto(valor) -> str:
    if valor is None:
        return ""

    return str(valor).strip()


def traducir_origen(valor: str) -> str:
    original = normalizar_texto(valor)

    if not original:
        return "Desconocido"

    clave = (
        original.lower()
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )

    equivalencias = {
        "face": "Reconocimiento facial",
        "facerecognition": "Reconocimiento facial",
        "facial": "Reconocimiento facial",
        "fingerprint": "Huella digital",
        "finger": "Huella digital",
        "fp": "Huella digital",
        "card": "Tarjeta",
        "tarjeta": "Tarjeta",
        "pin": "PIN",
        "password": "PIN",
        "qrcode": "Código QR",
        "qr": "Código QR",
        "mobile": "Aplicación móvil",
        "mobileapp": "Aplicación móvil",
        "app": "Aplicación móvil",
        "gps": "Aplicación móvil GPS",
        "mobileattendance": "Aplicación móvil",
        "manual": "Registro manual",
        "bluetooth": "Bluetooth",
    }

    if clave in equivalencias:
        return equivalencias[clave]

    if "mobile" in clave or "app" in clave:
        return "Aplicación móvil"

    if "gps" in clave:
        return "Aplicación móvil GPS"

    if "face" in clave or "facial" in clave:
        return "Reconocimiento facial"

    if "finger" in clave or clave.startswith("fp"):
        return "Huella digital"

    if "card" in clave or "tarjeta" in clave:
        return "Tarjeta"

    if "pin" in clave or "password" in clave:
        return "PIN"

    return original


def crear_registro(
    persona: dict,
    tipo: str,
    fecha: str,
    hora: str,
    fuente: str,
    dispositivo: str,
    area: str,
) -> dict | None:
    fecha = normalizar_texto(fecha)
    hora = normalizar_texto(hora)

    if not fecha and not hora:
        return None

    nombre = normalizar_texto(
        persona.get("fullName")
    )

    if not nombre:
        nombre = " ".join(
            parte
            for parte in [
                normalizar_texto(
                    persona.get("firstName")
                ),
                normalizar_texto(
                    persona.get("lastName")
                ),
            ]
            if parte
        )

    return {
        "numero_empleado": normalizar_texto(
            persona.get("personCode")
        ),
        "nombre": nombre,
        "fecha": fecha,
        "hora": hora,
        "tipo": tipo,
        "metodo": traducir_origen(fuente),
        "metodo_original": normalizar_texto(fuente),
        "dispositivo": normalizar_texto(dispositivo),
        "area": normalizar_texto(area),
        "departamento": normalizar_texto(
            persona.get("fullPath")
        ),
        "horas_trabajadas": normalizar_texto(
            persona.get("workDuration")
        ),
        "horas_extra": normalizar_texto(
            persona.get("overtimeDuration")
        ),
    }


def convertir_reporte_en_checadas(
    reportes: list[dict],
) -> list[dict]:
    registros = []

    for persona in reportes:
        entrada = crear_registro(
            persona=persona,
            tipo="Entrada",
            fecha=persona.get("clockInDate"),
            hora=persona.get("clockInTime"),
            fuente=persona.get("clockInSource"),
            dispositivo=persona.get("clockInDevice"),
            area=persona.get("clockInArea"),
        )

        salida = crear_registro(
            persona=persona,
            tipo="Salida",
            fecha=persona.get("clockOutDate"),
            hora=persona.get("clockOutTime"),
            fuente=persona.get("clockOutSource"),
            dispositivo=persona.get("clockOutDevice"),
            area=persona.get("clockOutArea"),
        )

        if entrada:
            registros.append(entrada)

        if salida:
            registros.append(salida)

    return registros


def descargar_asistencia(
    token: str,
    base_url: str,
    fecha: str,
) -> list[dict]:
    url = (
        f"{base_url}"
        "/api/hccgw/attendance/v1/report/"
        "totaltimecard/list"
    )

    inicio = f"{fecha}T00:00:00-07:00"
    fin = f"{fecha}T23:59:59-07:00"

    pagina = 1
    registros = []

    while True:
        print(f"Descargando página {pagina}...")

        payload = {
            "pageIndex": pagina,
            "pageSize": PAGE_SIZE,
            "beginTime": inicio,
            "endTime": fin,
            "personName": "",
            "personCode": "",
            "personGroupIds": [],
        }

        respuesta = post_json(
            url=url,
            payload=payload,
            token=token,
        )

        data = respuesta.get("data", {})
        reportes = data.get("reportDataList", [])

        if not isinstance(reportes, list):
            reportes = []

        registros.extend(
            convertir_reporte_en_checadas(reportes)
        )

        more_data = data.get("moreData", 0)

        if not reportes:
            break

        if str(more_data).lower() not in {
            "1",
            "true",
        }:
            break

        pagina += 1

    registros.sort(
        key=lambda registro: (
            registro["fecha"],
            registro["hora"],
            registro["nombre"],
        )
    )

    return registros


def guardar_csv(
    registros: list[dict],
    fecha: str,
) -> str:
    nombre_archivo = (
        f"checadas_hikconnect_{fecha}.csv"
    )

    columnas = [
        "numero_empleado",
        "nombre",
        "fecha",
        "hora",
        "tipo",
        "metodo",
        "metodo_original",
        "dispositivo",
        "area",
        "departamento",
        "horas_trabajadas",
        "horas_extra",
    ]

    with open(
        nombre_archivo,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as archivo:
        writer = csv.DictWriter(
            archivo,
            fieldnames=columnas,
        )
        writer.writeheader()
        writer.writerows(registros)

    return nombre_archivo


def mostrar_resumen(registros: list[dict]) -> None:
    print("\nRESULTADOS")
    print("-" * 85)

    if not registros:
        print("No se encontraron checadas.")
        return

    for registro in registros:
        print(
            f"{registro['hora']:8} | "
            f"{registro['tipo']:7} | "
            f"{registro['nombre'][:30]:30} | "
            f"{registro['metodo']}"
        )


def main() -> None:
    validar_configuracion()

    try:
        fecha = pedir_fecha()
        token, base_url = obtener_token()

        registros = descargar_asistencia(
            token=token,
            base_url=base_url,
            fecha=fecha,
        )

        mostrar_resumen(registros)

        archivo = guardar_csv(
            registros=registros,
            fecha=fecha,
        )

        print("\nProceso terminado.")
        print(f"Checadas encontradas: {len(registros)}")
        print(f"Archivo generado: {archivo}")

    except RuntimeError as exc:
        print(f"\nERROR:\n{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()