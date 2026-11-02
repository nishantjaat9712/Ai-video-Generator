[CmdletBinding()]
param([switch]$Production, [switch]$NoWait)
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Initialize-StudioEnvironment -ReadDotEnv
$manifestPath = Join-Path $root 'data\processes.json'
$lockPath = Join-Path $root 'data\startup.lock'
$startupLock = $null
$started = @()
try {
    try {
        $startupLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch { throw 'Another StoryOrbit start/stop operation is already in progress.' }
    if (Test-Path -LiteralPath $manifestPath) {
        $existing = @(Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json)
        $live = @($existing | Where-Object { Test-OwnedProcess $_ })
        if ($live.Count) { throw 'StoryOrbit has recorded running processes. Use scripts\Stop.ps1 before restarting.' }
    }
    $pythonExe = Join-Path $root '.venv\Scripts\python.exe'
    $nodeExe = Join-Path $root '.tools\node\node.exe'
    $nextJs = Join-Path $root 'frontend\node_modules\next\dist\bin\next'
    foreach ($required in @($pythonExe, $nodeExe, $nextJs, (Join-Path $root 'backend\app\main.py'), (Join-Path $root 'backend\app\worker.py'))) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Missing $required. Run scripts\Install.ps1 first." }
    }
    if ($Production -and -not (Test-Path -LiteralPath (Join-Path $root 'frontend\.next\BUILD_ID'))) {
        throw 'No production build exists. Run scripts\Install.ps1 without -SkipBuild first.'
    }
    foreach ($port in @(8000, 3000)) {
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $attempt = $client.ConnectAsync('127.0.0.1', $port)
            if ($attempt.Wait(300) -and $client.Connected) { throw "Port $port is already in use. Stop that service or adjust the application configuration." }
        } catch [AggregateException] { } finally { $client.Dispose() }
    }
    function Start-StudioProcess {
        param([string]$Name, [string]$Executable, [string]$Arguments, [string]$Directory)
        $logs = Join-Path $root 'data\logs'
        $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs ($Name + '.log')) -RedirectStandardError (Join-Path $logs ($Name + '.error.log'))
        $record = [pscustomobject]@{ name = $Name; id = $process.Id; executable = $Executable; started_ticks = $process.StartTime.ToUniversalTime().Ticks }
        Write-Host "$Name started (PID $($process.Id))."
        return $record
    }
    $started += Start-StudioProcess 'backend' $pythonExe '-m uvicorn app.main:app --host 127.0.0.1 --port 8000' (Join-Path $root 'backend')
    Write-ProcessManifest $started
    $started += Start-StudioProcess 'worker' $pythonExe '-m app.worker' (Join-Path $root 'backend')
    Write-ProcessManifest $started
    $mode = if ($Production) { 'start' } else { 'dev' }
    $started += Start-StudioProcess 'frontend' $nodeExe ('"' + $nextJs + '" ' + $mode + ' --hostname 127.0.0.1 --port 3000') (Join-Path $root 'frontend')
    Write-ProcessManifest $started
    if (-not $NoWait) {
        foreach ($endpoint in @('http://127.0.0.1:8000/api/health', 'http://127.0.0.1:3000')) {
            $ready = $false
            $deadline = [DateTime]::UtcNow.AddSeconds(60)
            while ([DateTime]::UtcNow -lt $deadline) {
                foreach ($record in $started) {
                    if (-not (Test-OwnedProcess $record)) { throw "$($record.name) exited during startup. Read data\logs\$($record.name).error.log." }
                }
                try {
                    $headers = @{}
                    if ($env:STORYORBIT_API_TOKEN) { $headers['Authorization'] = 'Bearer ' + $env:STORYORBIT_API_TOKEN }
                    $response = Invoke-WebRequest -Uri $endpoint -Headers $headers -UseBasicParsing -TimeoutSec 2
                    if ($response.StatusCode -eq 200) { $ready = $true; break }
                } catch { }
                Start-Sleep -Milliseconds 750
            }
            if (-not $ready) { throw "Startup timed out waiting for $endpoint. Read data\logs." }
            Write-Host "Ready: $endpoint"
        }
    }
    Write-Host 'Open http://127.0.0.1:3000. Stop with .\scripts\Stop.ps1.'
} catch {
    foreach ($record in $started) { Stop-OwnedProcessTree $record }
    if ($started.Count) { Write-ProcessManifest @() }
    throw
} finally {
    if ($startupLock) { $startupLock.Dispose() }
}
