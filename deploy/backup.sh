#!/usr/bin/env bash
set -euo pipefail

database_path="/var/lib/tiempo/checador.sqlite3"
backup_directory="/var/backups/tiempo"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${backup_directory}/checador-${timestamp}.sqlite3"

install -d -m 0750 -o tiempo -g tiempo "${backup_directory}"
sqlite3 "${database_path}" ".backup '${backup_path}'"
chown tiempo:tiempo "${backup_path}"
chmod 0640 "${backup_path}"

echo "Copia creada: ${backup_path}"

