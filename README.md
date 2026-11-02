# StoryOrbit AI Studio

A local Windows video studio for creating and editing AI-assisted video projects. Projects, uploaded assets, model outputs, credentials, portable runtimes and caches stay in this project.

## Open the studio

Double-click **Start Studio.cmd**, then open http://127.0.0.1:3000. Use **Stop Studio.cmd** to stop its three processes. First launch may take a minute. Logs are in `data/logs`.

PowerShell alternative:
```powershell
Set-Location 'C:\path\to\project'
.\scripts\Start.ps1 -Production
# Later:
.\scripts\Stop.ps1
```

For a fresh installation, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install.ps1`. This downloads dependencies to D: and builds the frontend. To check an existing install without downloads, add `-VerifyOnly`. Model weights are not included.

## What works without AI credentials

- Persistent project and character libraries, prompt/script drafts and scene editing.
- Validated image/video/audio uploads, reference-video technical measurements.
- Import scene clips, reorder and trim them, mix licensed background audio, export a validated MP4.
- Hindi/English scene-aligned subtitles as MP4 soft captions, SRT and browser WebVTT.
- The supplied MIT YouTube hook and title tools. Hindi hook scoring is explicitly unsupported.
- Durable background jobs, stage progress, cancellation, bounded retries, crash recovery, quotas and hardware diagnostics.

## Configure inference later

No AI provider was connected during development, as requested. No completed AI video or successful live model run is claimed.

In **Studio settings**, configure:
1. **fal API key** for paid Wan 2.2 text/image-to-video, or an independently tested **ComfyUI** server and workflow manifest.
2. An OpenAI-compatible chat endpoint and model for script planning.
3. A compatible speech endpoint for optional narration. Languages and voice support depend on that provider.
4. Optional Google OAuth credentials for YouTube. Publishing defaults off and every upload requires approval.

Save credentials privately in Settings, not in frontend source. Local credentials are encrypted with a key in `data/.credentials.key`; back up that key with the data, and restrict access to this folder. Encryption does not protect against someone with access to both files.

Standard local Wan configurations may exceed consumer GPU memory. Check the selected model and workflow requirements before configuring local inference. Cloud inference is the practical initial route and requires paid credits. See [model choices and architecture](docs/models-and-architecture.md) and [ComfyUI setup](workflows/README.md).

## First real generation acceptance check

After configuring a provider: create a project → enter a Hindi/English prompt → choose portrait settings supported by that provider → Generate Complete Video → inspect actual job status → preview/download the validated MP4 → reopen the saved project.

For a story, configure the LLM, create a scene plan in Script Studio, review each shot, then generate. Narration requires TTS; too-long speech produces an actionable timing error. Individual scene regeneration retains other clips. Render saved clips applies timeline changes without generating new video.

## Boundaries

This is a local application build, not a certified public SaaS deployment. Live model inference, live TTS, OAuth consent and real YouTube upload have not been verified. No simulated AI response, stock substitute or reference-video copy is used as generated output.

Character descriptions guide prompts. Saved reference images can be selected as an image-to-video input; automatic multi-character identity conditioning and reference-image generation are not implemented. Identity continuity is never guaranteed. Reference analysis measures dimensions, pacing-related frame differences and brightness; semantic style matching is not automated. Lip sync, speech forced alignment, generated sound effects, S3 storage and native 1080p inference are not implemented. Camera/style choices guide the prompt rather than guaranteeing motion. Caption timings are scene-aligned. Use music you own or license.

## Source structure

```text
frontend/       Next.js, React, TypeScript, Tailwind studio
backend/app/    FastAPI, SQLAlchemy, adapters, worker, media, creative, YouTube
backend/tests/  Isolated API/media tests and mocked provider contracts
scripts/        D-drive installer, launch/stop, hardware checks
workflows/      ComfyUI binding instructions and user workflows
references/     Original audited YouTube utilities and MIT license
docs/           Reference analysis, architecture and verification
data/           SQLite, assets, exports and encrypted settings
.tools/         Portable Node, Python, uv and FFmpeg
.cache/ .tmp/   Local caches and temporary test data
```

The local worker uses durable database jobs rather than requiring Redis on Windows. SQLite is the default; SQLAlchemy can use PostgreSQL via DATABASE_URL. Only one generation pipeline is claimed at once. Do not deploy multiple public workers without reviewing the deployment notes.

## Tests

```powershell
Set-Location 'C:\path\to\project'
$env:TEMP="$PWD\.tmp"; $env:TMP=$env:TEMP
Set-Location backend
..\.venv\Scripts\python.exe -m pytest tests -q --basetemp '..\.tmp\pytest-final'
Set-Location ..\frontend
..\.tools\node\npm.cmd run typecheck
..\.tools\node\npm.cmd run build
```

Tests use isolated storage. FFmpeg fixtures test actual media processing; mocked provider tests verify transport handling, not AI quality. See [Windows setup](docs/windows-setup.md), [deployment notes](docs/deployment.md), and [ZIP audit](docs/zip-audit.md).

## Attribution

YouTube writing utilities: copyright 2026 Jake Schincariol, MIT; original notice retained at `references/youtube-skills/youtube-agent-skill-main/LICENSE`. Tool heuristics do not predict virality. Model weights and third-party runtimes retain their own licenses. FFmpeg binaries are obtained from Gyan; review their included licenses before redistribution.
