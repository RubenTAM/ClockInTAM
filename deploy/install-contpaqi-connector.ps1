[CmdletBinding()]
param(
    [string]$SqlServer = "SERVIDOR\COMPAC",
    [string]$SqlDatabase = "ctTecno_DEV",
    [string]$SqlUser = "tiempo_integracion",
    [Parameter(Mandatory = $true)]
    [string]$TiempoBaseUrl,
    [string]$TaskName = "Tiempo-CONTPAQi-Vacaciones"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv-connector\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env.connector"

if ($SqlDatabase -cne "ctTecno_DEV") {
    throw "Esta versión solo puede instalarse para ctTecno_DEV."
}

$tiempoUri = [Uri]$TiempoBaseUrl
if ($tiempoUri.Scheme -cne "https") {
    throw "TiempoBaseUrl debe usar HTTPS. No se transmitirán datos privados por HTTP."
}

function Read-PlainSecret([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

$sqlPassword = Read-PlainSecret "Contraseña SQL del usuario de integración"
$tiempoToken = Read-PlainSecret "Token compartido de Tiempo"

Push-Location $projectRoot
try {
    py -3 -m venv .venv-connector
    & $pythonPath -m pip install --disable-pip-version-check -r requirements.txt

    @(
        "CONTPAQ_SQL_SERVER=$SqlServer"
        "CONTPAQ_SQL_PORT=1433"
        "CONTPAQ_SQL_DATABASE=$SqlDatabase"
        "CONTPAQ_SQL_USER=$SqlUser"
        "CONTPAQ_SQL_PASSWORD=$sqlPassword"
        "TIEMPO_BASE_URL=$($TiempoBaseUrl.TrimEnd('/'))"
        "TIEMPO_SYNC_TOKEN=$tiempoToken"
    ) | Set-Content -Path $envPath -Encoding UTF8

    icacls $envPath /inheritance:r | Out-Null
    icacls $envPath /grant:r "*S-1-5-18:(F)" "*S-1-5-32-544:(F)" | Out-Null

    $arguments = "-m nomina.apply_vacation_requests --env-file `"$envPath`""
    $action = New-ScheduledTaskAction `
        -Execute $pythonPath `
        -Argument $arguments `
        -WorkingDirectory $projectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable

    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings `
        -Description "Conector saliente cifrado de vacaciones y recibos bajo demanda." `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Conector instalado y activo: $TaskName"
}
finally {
    $sqlPassword = $null
    $tiempoToken = $null
    Pop-Location
}
