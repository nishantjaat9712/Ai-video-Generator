[CmdletBinding()]
param([switch]$SkipBuild, [switch]$Offline, [switch]$VerifyOnly)
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Initialize-StudioEnvironment
Set-Location -LiteralPath $root
$toolsDir = Join-Path $root '.tools'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($VerifyOnly) {
    $pythonExe = Join-Path $root '.venv\Scripts\python.exe'
    $nodeExe = Join-Path $root '.tools\node\node.exe'
    $uvExe = Join-Path $root '.tools\uv\uv.exe'
    foreach ($required in @($pythonExe,$nodeExe,$uvExe,(Join-Path $root 'frontend\node_modules\next\package.json'))) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Required dependency missing: $required. Run Install.ps1." }
    }
    & $nodeExe --version
    Assert-LastExit 'Node version check'
    & $uvExe pip check --python $pythonExe
    Assert-LastExit 'Python dependency consistency check'
    & $pythonExe -c 'import fastapi, sqlalchemy, pydantic, httpx, PIL, psutil, cryptography; print("Backend imports passed.")'
    Assert-LastExit 'Backend dependency import check'
    foreach ($mediaBinary in @($env:FFMPEG_PATH,$env:FFPROBE_PATH)) {
        if (-not $mediaBinary -or -not (Test-Path -LiteralPath $mediaBinary)) { throw 'FFmpeg or ffprobe is missing.' }
        & $mediaBinary -version | Select-Object -First 1
        Assert-LastExit 'Media tool check'
    }
    Set-Location -LiteralPath (Join-Path $root 'frontend')
    & $nodeExe -e 'console.log("Next.js " + require("next/package.json").version)'
    Assert-LastExit 'Frontend dependency check'
    Set-Location -LiteralPath $root
    Write-Host 'Installed dependencies verified. No dependency reinstall or network download was requested.'
    exit 0
}


function Download-Verified {
    param([string]$Url, [string]$Destination, [string]$ExpectedSha256)
    if ($ExpectedSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid expected SHA256.' }
    if (-not (Test-Path -LiteralPath $Destination) -or
        (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $ExpectedSha256) {
        if ($Offline) { throw "Offline mode: missing or invalid archive $Destination" }
        Write-Host "Downloading $([IO.Path]::GetFileName($Destination))"
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    }
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $ExpectedSha256) {
        throw "Checksum mismatch for $Destination. No archive was extracted."
    }
}

function Read-RemoteText {
    param([string]$Url)
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing
    if ($response.Content -is [byte[]]) { return [Text.Encoding]::UTF8.GetString($response.Content) }
    return [string]$response.Content
}

$nodeExe = Join-Path $toolsDir 'node\node.exe'
if (-not (Test-Path -LiteralPath $nodeExe)) {
    $nodeArchive = Join-Path $toolsDir 'node-v24.22.1-win-x64.zip'
    Download-Verified 'https://nodejs.org/dist/v24.22.1/node-v24.22.1-win-x64.zip' $nodeArchive 'd2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5'
    Expand-Archive -LiteralPath $nodeArchive -DestinationPath $toolsDir -Force
    Rename-Item -LiteralPath (Join-Path $toolsDir 'node-v24.22.1-win-x64') -NewName 'node'
}
$nodeVersion = & $nodeExe --version
Assert-LastExit 'Node version check'
if ($nodeVersion -ne 'v24.22.1') { Write-Warning "Using installed Node $nodeVersion; validated installer target is v24.22.1." }

$uvExe = Join-Path $toolsDir 'uv\uv.exe'
if (-not (Test-Path -LiteralPath $uvExe)) {
    if ($Offline) { throw 'Offline mode requires the existing portable uv installation.' }
    $uvBase = 'https://github.com/astral-sh/uv/releases/download/0.13.6/uv-x86_64-pc-windows-msvc.zip'
    $uvHash = 'a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f'
    $uvArchive = Join-Path $toolsDir 'uv-0.13.6.zip'
    Download-Verified $uvBase $uvArchive $uvHash
    Expand-Archive -LiteralPath $uvArchive -DestinationPath (Join-Path $toolsDir 'uv') -Force
}
& $uvExe --version
Assert-LastExit 'uv version check'
$uvArgs = @()
if ($Offline) { $uvArgs += '--offline' }
& $uvExe @uvArgs python install 3.12.16
Assert-LastExit 'Python installation'
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $uvExe @uvArgs venv --python 3.12.16 (Join-Path $root '.venv')
    Assert-LastExit 'Python virtual environment creation'
}
$requirements = Join-Path $root 'backend\requirements.lock.txt'
if (-not (Test-Path -LiteralPath $requirements)) { $requirements = Join-Path $root 'backend\requirements.txt' }
& $uvExe @uvArgs pip install --python $pythonExe -r $requirements
Assert-LastExit 'Backend dependency installation'

$ffmpeg = @(Get-ChildItem -LiteralPath (Join-Path $toolsDir 'ffmpeg') -Filter ffmpeg.exe -Recurse -File -ErrorAction SilentlyContinue)
if (-not $ffmpeg.Count) {
    $ffmpegArchive = Join-Path $toolsDir 'ffmpeg-9.0.2-essentials_build.zip'
    Download-Verified 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip' $ffmpegArchive '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
    Expand-Archive -LiteralPath $ffmpegArchive -DestinationPath (Join-Path $toolsDir 'ffmpeg') -Force
}
$null = Initialize-StudioEnvironment
& $env:FFMPEG_PATH -version | Select-Object -First 1
Assert-LastExit 'FFmpeg check'

Set-Location -LiteralPath (Join-Path $root 'frontend')
$npm = Join-Path $toolsDir 'node\npm.cmd'
$npmArgs = @('ci', '--no-audit', '--no-fund')
if ($Offline) { $npmArgs += '--offline' }
& $npm @npmArgs
Assert-LastExit 'Frontend dependency installation'
& $npm run typecheck
Assert-LastExit 'TypeScript check'
if (-not $SkipBuild) {
    & $npm run build
    Assert-LastExit 'Next.js production build'
}
Set-Location -LiteralPath (Join-Path $root 'backend')
& $pythonExe -c 'from app import models, youtube; from app.db import init_db; init_db(); print("Database ready.")'
Assert-LastExit 'Database initialization'
Set-Location -LiteralPath $root
Write-Host "Installed in $root. Run .\scripts\Start.ps1. Inference models/accounts are configured separately."
