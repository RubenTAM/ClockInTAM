# Tiempo — control de horas extra

Aplicación web interna para registrar autorizaciones de horas extra y
compararlas con la asistencia consultada directamente desde Hik-Connect.

La aplicación **no es un sistema de nómina** y no conserva las checadas.
SQLite almacena únicamente usuarios, autorizaciones y el historial de cambios.
Los datos de asistencia tienen una caché temporal en memoria de cinco minutos.

## Funciones incluidas

- Configuración segura del primer administrador.
- Acceso para varios usuarios.
- Consulta de trabajadores desde Hik-Connect.
- Autorizaciones para varias personas y días.
- Comparación semanal de horas autorizadas y no autorizadas.
- Semana operativa de TecnoAll de jueves a miércoles.
- Detección de permisos no utilizados y tiempo excedente.
- Exportación del reporte a CSV compatible con Excel.
- Historial interno de creación, modificación y eliminación.
- Interfaz adaptable a computadora, tableta y teléfono.

## Funcionamiento del cálculo

Hik-Connect entrega la entrada y la salida. Tiempo calcula las horas extra de
acuerdo con el horario general de TecnoAll: antes de las 08:00 y después de las
17:00 de lunes a viernes; antes de las 08:30 y después de las 13:00 los sábados;
y toda la jornada del domingo. Luego calcula la intersección con el horario
autorizado. El resultado separa:

- tiempo dentro de autorización;
- tiempo excedente;
- horas extra sin autorización;
- autorización no utilizada.

El nombre normalizado es la referencia principal. Se conservan el nombre
original y los acentos para mostrarlos en pantalla.

## Ejecución local

Se requiere Python 3.9 o posterior.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Completa en `.env` las credenciales de Hik-Connect y genera una clave para las
sesiones:

```bash
openssl rand -hex 32
```

Después inicia la aplicación:

```bash
flask --app app run
```

Abre `http://127.0.0.1:5000`. En el primer acceso la aplicación pedirá crear al
administrador principal.

## Pruebas

```bash
python -m unittest discover -v
```

## Producción

Los archivos de `deploy/` contienen ejemplos para el servicio de Linux, Nginx
y las copias de seguridad en el Droplet. En producción deben definirse:

- `APP_ENV=production`
- `SECRET_KEY`
- `DATABASE_PATH=/var/lib/tiempo/checador.sqlite3`
- credenciales de Hik-Connect
- `SESSION_COOKIE_SECURE=1` una vez habilitado HTTPS

Nunca se deben subir `.env`, la base SQLite ni sus copias de seguridad al
repositorio.
