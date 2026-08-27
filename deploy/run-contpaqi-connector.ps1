[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,
    [Parameter(Mandatory = $true)]
    [string]$EnvPath,
    [Parameter(Mandatory = $true)]
    [string]$SecretsPath
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security

function Unprotect-LocalMachineSecret([string]$Value) {
    $protected = [Convert]::FromBase64String($Value)
    $bytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $protected,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    return [System.Text.Encoding]::UTF8.GetString($bytes)
}

Get-Content -Path $EnvPath | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable(
            $matches[1].Trim(),
            $matches[2].Trim(),
            "Process"
        )
    }
}

$secrets = Get-Content -Raw -Path $SecretsPath | ConvertFrom-Json
$env:CONTPAQ_SQL_PASSWORD = Unprotect-LocalMachineSecret $secrets.SqlPassword
$env:TIEMPO_SYNC_TOKEN = Unprotect-LocalMachineSecret $secrets.TiempoToken

try {
    & $PythonPath -m nomina.apply_vacation_requests --watch --interval 5
    exit $LASTEXITCODE
}
finally {
    $env:CONTPAQ_SQL_PASSWORD = $null
    $env:TIEMPO_SYNC_TOKEN = $null
}
