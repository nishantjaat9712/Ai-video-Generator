[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Initialize-StudioEnvironment
$manifestPath = Join-Path $root 'data\processes.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Host 'No StoryOrbit process manifest exists.'
    exit 0
}
$startupLock = $null
try {
    try {
        $startupLock = [IO.File]::Open((Join-Path $root 'data\startup.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch { throw 'Another StoryOrbit start/stop operation is already in progress.' }
    $records = @(Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json)
    [array]::Reverse($records)
    foreach ($record in $records) {
        Stop-OwnedProcessTree $record
        Write-Host "Checked $($record.name) (PID $($record.id))."
    }
    $remaining = @($records | Where-Object { Test-OwnedProcess $_ })
    Write-ProcessManifest $remaining
    if ($remaining.Count) { throw 'Some owned processes are still running. Retry Stop.ps1 and inspect data\logs.' }
    Write-Host 'Recorded StoryOrbit processes stopped. Projects and outputs remain on D:.'
} finally {
    if ($startupLock) { $startupLock.Dispose() }
}
