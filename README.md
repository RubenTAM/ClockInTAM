# Tiempo — control de horas extra

Aplicación web interna para registrar autorizaciones de horas extra y
compararlas con la asistencia consultada directamente desde Hik-Connect.

La aplicación **no es un sistema de nómina** y no conserva las checadas.
SQLite almacena usuarios, autorizaciones, el catálogo de trabajadores con su
área asignada y el historial de cambios. Los datos de asistencia tienen una
caché temporal en memoria de cinco minutos.

## Funciones incluidas

- Configuración segura del primer administrador.
- Acceso para varios usuarios.
- Consulta de trabajadores desde Hik-Connect.
- Catálogo persistente de trabajadores y asignación por área.
- Búsqueda y filtro de trabajadores por área.
- Autorizaciones para varias personas y días.
- Comparación semanal de horas autorizadas y no autorizadas.
- Semana operativa de TecnoAll de jueves a miércoles.
- Detección de permisos no utilizados y tiempo excedente.
- Exportación del reporte a CSV compatible con Excel.
- Historial interno de creación, modificación y eliminación.
- Interfaz adaptable a computadora, tableta y teléfono.
- Visualización del último saldo de vacaciones sincronizado desde CONTPAQi.

## Saldos de vacaciones desde CONTPAQi

La aplicación recibe los saldos mediante un sincronizador ejecutado dentro de
la red de TecnoAll. SQL Server no debe publicarse en Internet. El sincronizador
consulta únicamente `NOM10001`, `NOM10014` y `NOM10051`, calcula el saldo a la
fecha de corte y envía a Tiempo el código de empleado, los días disponibles y
la fecha de actualización. La misma sincronización reconstruye la tarjeta de
vacaciones con aniversarios, periodos tomados y saldo progresivo para mostrarla
al trabajador. Para mantenerla compacta, solo se envía el aniversario laboral
más reciente y los movimientos posteriores. Tiempo conserva una copia de esa
tarjeta. Las solicitudes aprobadas entran a una cola durable y un conector
dentro de la red de TecnoAll las aplica de forma idempotente en `NOM10014`;
una solicitud rechazada nunca entra a la cola.

El código se mantiene separado por dominio: `checador/` contiene Hikvision,
asistencia y reportes operativos; `nomina/` contiene únicamente la lectura y
sincronización con CONTPAQi. Ambos módulos alimentan el mismo dashboard.

Usa un inicio de sesión SQL exclusivo: lectura para la sincronización y los
permisos mínimos de escritura requeridos sobre `NOM10014` para el conector.
Nunca configures la cuenta `sa` en la instalación permanente.

Variables requeridas en el equipo local:

```dotenv
CONTPAQ_SQL_SERVER=servidor-o-ip
CONTPAQ_SQL_PORT=1433
CONTPAQ_SQL_DATABASE=nombre-exacto-de-la-base
CONTPAQ_SQL_USER=tiempo_lectura
CONTPAQ_SQL_PASSWORD=clave-del-usuario-de-lectura
TIEMPO_BASE_URL=https://tiempo.ejemplo.com
TIEMPO_SYNC_TOKEN=token-largo-compartido
```

El servidor web de Tiempo solo necesita `CONTPAQ_SYNC_TOKEN`, con el mismo
valor que `TIEMPO_SYNC_TOKEN`. Para validar la consulta sin enviar datos:

```bash
python -m nomina.sync_contpaqi_vacations --dry-run --as-of 2026-08-25
```

Antes de programar la sincronización automática, compara los resultados con el
reporte **Vacaciones pendientes por empleado** de CONTPAQi, usando la misma
fecha de corte y la opción sin proporción del año en curso.

La primera versión del conector tiene un bloqueo intencional y solo acepta la
base `ctTecno_DEV`. Para ejecutar una sola consulta de la cola:

```bash
python -m nomina.apply_vacation_requests
```

Para dejarlo activo dentro de la red local:

```bash
python -m nomina.apply_vacation_requests --watch --interval 30
```

Si la comunicación con Tiempo falla después de insertar, el siguiente intento
reconoce el mismo empleado y periodo, reutiliza el folio y evita duplicarlo.

En el servidor Windows, abre PowerShell como administrador desde la carpeta
del proyecto y ejecuta el instalador. Solicita las dos claves sin mostrarlas,
protege el archivo local de configuración y registra una tarea cada minuto:

```powershell
.\deploy\install-contpaqi-connector.ps1 `
  -SqlServer "SERVIDOR\COMPAC" `
  -TiempoBaseUrl "https://tu-dominio-de-tiempo"
```

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
