[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'Common.ps1')
$root = Initialize-StudioEnvironment
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run scripts\Install.ps1 first.' }
@'
import json, shutil, subprocess, sys
from pathlib import Path
import psutil
root = Path(sys.argv[1])
result = {
    "project": str(root),
    "python": sys.version.split()[0],
    "cpu_logical_cores": psutil.cpu_count(),
    "ram_total_gb": round(psutil.virtual_memory().total / 1024**3, 2),
    "ram_available_gb": round(psutil.virtual_memory().available / 1024**3, 2),
    "disk_free_gb": round(shutil.disk_usage(root).free / 1024**3, 2),
    "gpu": [],
    "pytorch_cuda_inference": "Not verified; nvidia-smi only checks driver/device visibility."
}
smi = shutil.which("nvidia-smi")
if smi:
    try:
        process = subprocess.run([smi, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15)
        if process.returncode == 0:
            for line in process.stdout.splitlines():
                name, total, free, driver = [v.strip() for v in line.split(",")]
                result["gpu"].append({"name":name, "vram_total_mib":int(total), "vram_free_mib":int(free), "driver":driver})
        else:
            result["gpu_error"] = "nvidia-smi could not query the GPU."
    except Exception as exc:
        result["gpu_error"] = str(exc)
else:
    result["gpu_error"] = "nvidia-smi not available."
result["local_model_note"] = "The documented Wan2.2 TI2V-5B reference path needs at least 24 GB VRAM. Lower-memory custom workflows must be verified individually. Hosted inference uses paid credits."
print(json.dumps(result, indent=2))
'@ | & $pythonExe - $root
Assert-LastExit 'Hardware diagnostics'
