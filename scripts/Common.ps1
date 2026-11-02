Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-StudioRoot {
    $root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
    if (-not $root.StartsWith('D:\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'This installation keeps project files and caches on D:. Move the complete project to D: before running these scripts.'
    }
    return $root
}

function Initialize-StudioEnvironment {
    param([switch]$ReadDotEnv)
    $root = Get-StudioRoot
    foreach ($relative in @('.tools', '.cache', '.tmp', 'data', 'data\logs', 'workflows')) {
        New-Item -ItemType Directory -Path (Join-Path $root $relative) -Force | Out-Null
    }
    if ($ReadDotEnv) {
        $envFile = Join-Path $root '.env'
        if (Test-Path -LiteralPath $envFile) {
            foreach ($line in Get-Content -LiteralPath $envFile) {
                $trimmed = $line.Trim()
                if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
                if ($trimmed -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                    throw 'Invalid .env entry. Use NAME=value without PowerShell commands.'
                }
                $name, $value = $Matches[1], $Matches[2].Trim()
                if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
                [Environment]::SetEnvironmentVariable($name, $value, 'Process')
            }
        }
    }
    # Reapply storage/cache paths after .env so the D-drive requirement cannot be bypassed.
    $env:TEMP = Join-Path $root '.tmp'
    $env:TMP = $env:TEMP
    $env:UV_CACHE_DIR = Join-Path $root '.cache\uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.tools\python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $root '.tools\python-bin'
    $env:PIP_CACHE_DIR = Join-Path $root '.cache\pip'
    $env:npm_config_cache = Join-Path $root '.cache\npm'
    $env:HF_HOME = Join-Path $root '.cache\huggingface'
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $root '.cache\huggingface\hub'
    $env:TORCH_HOME = Join-Path $root '.cache\torch'
    $env:XDG_CACHE_HOME = Join-Path $root '.cache'
    $env:MPLCONFIGDIR = Join-Path $root '.cache\matplotlib'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    $env:NEXT_TELEMETRY_DISABLED = '1'
    $env:STORYORBIT_DATA_DIR = Join-Path $root 'data'
    if (-not $env:DATABASE_URL) {
        $env:DATABASE_URL = 'sqlite:///' + ((Join-Path $root 'data\storyorbit.db').Replace('\','/'))
    } elseif ($env:DATABASE_URL.StartsWith('sqlite:')) {
        $sqlitePath = $env:DATABASE_URL -replace '^sqlite:///',''
        if (-not $sqlitePath.StartsWith('D:', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'SQLite DATABASE_URL must use an absolute D: path. PostgreSQL uses a configured remote database.'
        }
    }

    $env:PATH = (Join-Path $root '.tools\node') + ';' + (Join-Path $root '.venv\Scripts') + ';' + $env:PATH
    $ffmpegRoot = Join-Path $root '.tools\ffmpeg'
    if (Test-Path -LiteralPath $ffmpegRoot) {
        $ffmpeg = Get-ChildItem -LiteralPath $ffmpegRoot -Filter ffmpeg.exe -File -Recurse | Select-Object -First 1
        if ($ffmpeg) {
            $env:FFMPEG_PATH = $ffmpeg.FullName
            $env:FFPROBE_PATH = Join-Path $ffmpeg.DirectoryName 'ffprobe.exe'
            $env:PATH = $ffmpeg.DirectoryName + ';' + $env:PATH
        }
    }
    if (-not $env:BACKEND_URL) { $env:BACKEND_URL = 'http://127.0.0.1:8000' }
    return $root
}

function Assert-LastExit {
    param([string]$Action)
    if ($LASTEXITCODE -ne 0) { throw "$Action failed with exit code $LASTEXITCODE." }
}

function Test-OwnedProcess {
    param($Record)
    try {
        $process = Get-Process -Id ([int]$Record.id) -ErrorAction Stop
        $expected = [IO.Path]::GetFullPath([string]$Record.executable)
        if (-not [string]::Equals($process.Path, $expected, [StringComparison]::OrdinalIgnoreCase)) { return $false }
        return $process.StartTime.ToUniversalTime().Ticks -eq [long]$Record.started_ticks
    } catch { return $false }
}

function Stop-OwnedProcessTree {
    param($Record)
    if (-not (Test-OwnedProcess $Record)) {
        Write-Warning "Skipping PID $($Record.id): it is absent or does not match the recorded executable and start time."
        return
    }
    $root = Get-StudioRoot
    $descendants = @()
    try {
        $processTable = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        $pending = @([int]$Record.id)
        $seen = @{}
        while ($pending.Count -gt 0) {
            $parentId = $pending[0]
            $pending = @($pending | Select-Object -Skip 1)
            if ($seen.ContainsKey($parentId)) { continue }
            $seen[$parentId] = $true
            foreach ($candidate in $processTable | Where-Object { $_.ParentProcessId -eq $parentId }) {
                $pending += [int]$candidate.ProcessId
                try {
                    $child = Get-Process -Id $candidate.ProcessId -ErrorAction Stop
                    if ($child.Path -and $child.Path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -and
                        $child.StartTime.ToUniversalTime().Ticks -ge [long]$Record.started_ticks) {
                        $descendants += [pscustomobject]@{
                            id = $child.Id; executable = $child.Path
                            started_ticks = $child.StartTime.ToUniversalTime().Ticks
                        }
                    }
                } catch { }
            }
        }
    } catch {
        Write-Warning 'Could not enumerate child processes. Only the verified recorded process will be stopped.'
    }
    [array]::Reverse($descendants)
    foreach ($childRecord in $descendants) {
        if (Test-OwnedProcess $childRecord) {
            Stop-Process -Id $childRecord.id -ErrorAction SilentlyContinue
        }
    }
    if (Test-OwnedProcess $Record) {
        Stop-Process -Id $Record.id -ErrorAction SilentlyContinue
        Wait-Process -Id $Record.id -Timeout 5 -ErrorAction SilentlyContinue
    }
}

function Write-ProcessManifest {
    param([array]$Processes)
    $root = Get-StudioRoot
    $path = Join-Path $root 'data\processes.json'
    $temporary = Join-Path $root 'data\processes.json.tmp'
    ConvertTo-Json -InputObject @($Processes) -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $path -Force
}
