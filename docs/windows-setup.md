# Windows setup and troubleshooting

Run commands from the project root. Launch scripts use portable tools, project-only environment variables, and hidden processes. They do not modify the Windows registry, user PATH, or global PowerShell policy.

- Existing installation: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install.ps1 -VerifyOnly`.
- Install dependencies and build: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install.ps1`.
- Start: double-click `Start Studio.cmd`.
- Stop: double-click `Stop Studio.cmd`.
- GPU/RAM/disk checks: `.\scripts\Check-Hardware.ps1`.

Node, uv, Python and FFmpeg are installed under .tools; Python packages under .venv; Node packages under frontend/node_modules. SQLite is initialized automatically. Model files are separate: follow [the ComfyUI guide](../workflows/README.md). A GPU detected by nvidia-smi is not proof of a working CUDA/PyTorch model installation.

## Troubleshooting

**Port in use:** startup refuses ports 3000/8000 already owned by another process. Use Stop Studio.cmd for this app; do not kill unrelated applications.

**Missing build:** run Install.ps1 or use the portable npm command to build frontend. The production launcher checks for .next/BUILD_ID.

**No provider configured:** save a funded fal key or configure an independently tested ComfyUI server. Drafts and local editing remain available. Never paste credentials into chats or frontend source.

**Missing FFmpeg:** rerun installation. Both FFmpeg and ffprobe must be present.

**Server offline:** inspect data/logs/backend.error.log, worker.error.log, frontend.error.log. Run Stop then Start.

**CUDA out of memory:** reduce workload only to settings your workflow supports or use a bigger/cloud GPU. Check the selected model's hardware requirements before retrying.

**Narration too long:** shorten the line or use a longer model-supported scene. The renderer rejects speech that cannot fit; it does not silently cut words.

**Job needs review:** a provider submission may have succeeded while the response was lost. Check its dashboard before approving a retry, which may cost extra.

**Settings cannot decrypt:** restore data/.credentials.key together with settings.json from the same backup. Keep this key private.

**Public mode authentication errors:** this build is intended for local use. See deployment.md before exposing any port.

Back up data and workflows when the app is stopped. Keep original reference files. The installer does not remove project data. Tests write under .tmp and create their own databases; they do not use your projects.
