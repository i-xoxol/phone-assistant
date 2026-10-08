param(
    [ValidateSet('start', 'restart', 'stop', 'status')]
    [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
$phoneRoot = Split-Path -Parent $PSScriptRoot
$phonePython = Join-Path $phoneRoot '.venv\Scripts\python.exe'
$phoneData = Join-Path $phoneRoot 'data'
$phoneRuntimePath = Join-Path $phoneData 'runtime.json'
$phoneRuntime = if (Test-Path -LiteralPath $phoneRuntimePath) {
    Get-Content -LiteralPath $phoneRuntimePath -Raw | ConvertFrom-Json
} else { [pscustomobject]@{} }

function Get-PhoneListener {
    Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

$phoneListener = Get-PhoneListener
if ($Action -eq 'status') {
    if ($phoneListener) {
        Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' | ConvertTo-Json -Compress
    } else { Write-Output 'Local service is stopped.' }
    exit
}

if ($phoneListener) {
    $phoneOwner = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $phoneListener.OwningProcess)
    if (-not $phoneOwner.CommandLine.Contains($phonePython) -or
        $phoneOwner.CommandLine -notlike '*-m uvicorn app.main:app*') {
        throw 'Port 8000 is owned by another process; nothing changed.'
    }
    if ($Action -eq 'start') { Write-Output 'Local service is already running.'; exit }
    Stop-Process -Id $phoneOwner.ProcessId
    for ($phoneAttempt = 0; $phoneAttempt -lt 20 -and (Get-PhoneListener); $phoneAttempt++) {
        Start-Sleep -Milliseconds 200
    }
}
if ($Action -eq 'stop') { Write-Output 'Local service stopped. Tunnel remains running.'; exit }

New-Item -ItemType Directory -Path $phoneData -Force | Out-Null
$phoneProcess = Start-Process -FilePath $phonePython -ArgumentList (
    '-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log'
) -WorkingDirectory $phoneRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $phoneData 'service.stdout.log') `
    -RedirectStandardError (Join-Path $phoneData 'service.stderr.log')
$phoneRuntime | Add-Member -NotePropertyName service_pid -NotePropertyValue $phoneProcess.Id -Force
for ($phoneAttempt = 0; $phoneAttempt -lt 30; $phoneAttempt++) {
    Start-Sleep -Milliseconds 200
    $phoneListener = Get-PhoneListener
    if ($phoneListener) {
        $phoneRuntime | Add-Member -NotePropertyName service_listener_pid -NotePropertyValue $phoneListener.OwningProcess -Force
        $phoneRuntime | ConvertTo-Json | Set-Content -LiteralPath $phoneRuntimePath
        Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' | ConvertTo-Json -Compress
        exit
    }
}
throw 'Service did not become ready; inspect data/service.stderr.log.'
