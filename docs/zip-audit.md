# Audit of the uploaded YouTube skills ZIP

Inspected 29 October 2026. Input: `D:\Ai video generator\youtube-agent-skill-main.zip` (38,702 bytes). Safely extracted within `references/youtube-skills/`; absolute path escape and uncompressed-size checks were applied before extraction.

The README, all eleven `SKILL.md` files, all six Python utility sources, hook formulas and license were inspected. These files are source material for the application; their interactive assistant instructions do not authorize external posting or account access.

## What is present

| Module | Actual reusable material | What must be added |
|---|---|---|
| yt-script | 21 hook formulas, script/retention-beat workflow, `hookscore.py` English heuristic | Configured LLM to write scripts, validated scene schema, Hindi-aware evaluation |
| yt-package | Title/thumbnail pairing instructions and `title.py` linter | LLM metadata drafts, thumbnail image generation or design |
| yt-edit | `deadair.py` reads timestamped SRT/VTT/Whisper-object JSON and emits proposed cuts | Speech transcription and actual FFmpeg media edits |
| yt-chapters | `chapters.py` proposes transcript boundaries and checks minimum chapter conditions | Better chapter titles; chapters are usually irrelevant to a ten-second short |
| yt-retention | `retention.py` analyzes supplied two-column CSV | Authorized Analytics data import, explicit axis interpretation and robust validation |
| yt-viral | `swipe.py` ranks supplied video metadata by channel median; skips channels with fewer than four videos | User-supplied or authorized data acquisition; no scraper is included |
| yt-seo | Description/query/tag writing instructions | LLM and product UI; no SEO generation service or ranking guarantee |
| yt-shorts | Instructions for choosing self-contained transcript spans and rewriting openings | Transcription, scene selection, cropping, video rendering |
| yt-plan | Human-readable capacity-based content calendar workflow | Persistent schedule, approval controls and an actual scheduler |
| yt-comment | Comment triage and reply-writing instructions | Authorized comment retrieval/posting; out of initial generation scope |
| yt-audit | Channel review workflow | Real channel data and model-supported visual/text analysis |

There is also a reusable channel voice-profile template. No video model, image generator, TTS engine, lip-sync model, OAuth integration, YouTube upload API, background scheduler, GPU management, or production website is included. The README explicitly states that the pack does not publish and does not edit media.

## Verification and limitations

All six utilities executed successfully against clearly labeled local test fixtures using Python 3.12.16 on D. These were parser/heuristic smoke checks, not validation of audience predictions or production robustness. Exact command outputs are preserved in `zip-tool-checks.json`.

- Hook and title tokenizers use English ASCII vocabulary/regular expressions. Do not score Devanagari Hindi as if it were supported; label Hinglish results as limited heuristics. Scores cannot predict virality or retention.
- `title.py` warns about thumbnail length only above four words although its prose recommends three. Treat the prose as editorial guidance, not a strict implemented limit.
- `deadair.py` calls `.get()` on JSON before checking whether it is an array; raw array JSON is not reliably supported. Pass a Whisper-style `{ "segments": [...] }` object or SRT/VTT, or fix and test an adapter.
- Retention axis units are inferred from maximum values; a short video in seconds can be mistaken for a percent axis. Product integration should require explicit units rather than silently inherit this assumption.
- The tools do not authenticate, call AI APIs, fetch channel metrics, publish, or modify source media. Treat generated edit lists and draft metadata as reviewable suggestions.
- Runtime estimates and claims in prompt instructions are editorial heuristics, not measured facts about the user's channel. Do not invent metrics or promise performance.

## License and attribution

The archive is MIT licensed: **Copyright (c) 2026 Jake Schincariol**. Keep the supplied `LICENSE` with copied or substantially reused code and documentation. The untouched archive extraction retains that notice. Application adapters should identify the source and preserve its license; application code may have its own license separately.

Suggested integration order: hook/title analysis and formula-assisted script prompts first; SEO and Shorts planning next; transcript/retention tooling when corresponding real data is available. YouTube OAuth, uploads and scheduling are separate new features and must remain disabled until explicitly configured and approved.
