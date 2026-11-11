param(
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Status',
    [string]$BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$Port = 8787
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$webScript = Join-Path $projectRoot 'web_app.py'
$dataDirectory = Join-Path $projectRoot 'data'
$processRecord = Join-Path $dataDirectory 'web-process.json'
$existingProcess = $null

function Stop-WebProcessTree($Process) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Process.ProcessId)" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        if ($child.CommandLine -and $child.CommandLine.Contains($webScript)) {
            Stop-WebProcessTree $child
        }
    }
    Stop-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $processRecord) {
    $savedProcess = Get-Content -LiteralPath $processRecord -Raw | ConvertFrom-Json
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $($savedProcess.process_id)" -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.CommandLine -and $candidate.CommandLine.Contains($webScript)) {
        $existingProcess = $candidate
    }
}

if ($Action -eq 'Status') {
    if ($existingProcess) {
        Write-Output "Running: PID $($existingProcess.ProcessId), http://$($savedProcess.bind_address):$($savedProcess.port), log data/web.log"
    } else {
        Write-Output 'Stopped'
    }
    exit 0
}

if ($Action -eq 'Stop') {
    if ($existingProcess) {
        Stop-WebProcessTree $existingProcess
        Write-Output "Stopped: PID $($existingProcess.ProcessId)"
    } else {
        Write-Output 'Already stopped'
    }
    if (Test-Path -LiteralPath $processRecord) { Remove-Item -LiteralPath $processRecord }
    exit 0
}

if ($existingProcess) {
    Write-Output "Already running: PID $($existingProcess.ProcessId)"
    exit 0
}

$pythonCommand = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonCommand)) {
    throw 'Install dependencies first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
# Only one scheduler should own the existing mappings.
& (Join-Path $projectRoot 'manage_sync.ps1') -Action Stop
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
$webArguments = @('-X', 'utf8', ('"' + $webScript + '"'), '--host', $BindAddress, '--port', $Port)
$webProcess = Start-Process -FilePath $pythonCommand -ArgumentList $webArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardError (Join-Path $dataDirectory 'web.log') -RedirectStandardOutput (Join-Path $dataDirectory 'web.stdout.log') -PassThru
$serverProcessId = $webProcess.Id
# Windows venv python.exe can be a redirector; record the actual server child.
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    $serverChild = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($webProcess.Id)" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($webScript) } | Select-Object -First 1
    if ($serverChild) { $serverProcessId = $serverChild.ProcessId; break }
    if ($webProcess.HasExited) { break }
    Start-Sleep -Milliseconds 150
}
@{ process_id = $serverProcessId; started_at = (Get-Date).ToString('o'); bind_address = $BindAddress; port = $Port } | ConvertTo-Json | Set-Content -LiteralPath $processRecord -Encoding UTF8
Write-Output "Started: PID $serverProcessId, http://${BindAddress}:$Port"
