param(
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Status'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$syncScript = Join-Path $projectRoot 'sync_sub2api.py'
$dataDirectory = Join-Path $projectRoot 'data'
$processRecord = Join-Path $dataDirectory 'daemon-process.json'
$existingProcess = $null

if (Test-Path -LiteralPath $processRecord) {
    $savedProcess = Get-Content -LiteralPath $processRecord -Raw | ConvertFrom-Json
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $($savedProcess.process_id)" -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.CommandLine -and $candidate.CommandLine.Contains($syncScript)) {
        $existingProcess = $candidate
    }
}

if ($Action -eq 'Status') {
    if ($existingProcess) {
        Write-Output "Running: PID $($existingProcess.ProcessId), interval from data/web/config.json, log data/daemon.log"
    } else {
        Write-Output 'Stopped'
    }
    exit 0
}

if ($Action -eq 'Stop') {
    if ($existingProcess) {
        Stop-Process -Id $existingProcess.ProcessId
        Write-Output "Stopped: PID $($existingProcess.ProcessId)"
    } else {
        Write-Output 'Already stopped'
    }
    if (Test-Path -LiteralPath $processRecord) {
        Remove-Item -LiteralPath $processRecord
    }
    exit 0
}

if ($existingProcess) {
    Write-Output "Already running: PID $($existingProcess.ProcessId)"
    exit 0
}

New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
$pythonCommand = Join-Path $projectRoot '.venv\Scripts\python.exe'
$daemonArguments = @('-X', 'utf8', ('"' + $syncScript + '"'), '--daemon', '--apply', '--data-dir', ('"' + (Join-Path $dataDirectory 'web') + '"'))
$daemonProcess = Start-Process -FilePath $pythonCommand -ArgumentList $daemonArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardError (Join-Path $dataDirectory 'daemon.log') -RedirectStandardOutput (Join-Path $dataDirectory 'daemon.stdout.log') -PassThru
@{ process_id = $daemonProcess.Id; started_at = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $processRecord -Encoding UTF8
Write-Output "Started: PID $($daemonProcess.Id), log data/daemon.log"
