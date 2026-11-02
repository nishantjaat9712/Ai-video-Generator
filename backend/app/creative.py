"""Configured LLM/TTS calls and audited YouTube writing utilities; no simulated AI."""
from __future__ import annotations
import importlib.util, json, re, uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from .config import PROJECT_ROOT, read_settings, redact, validate_service_url
from .db import SessionLocal
from .models import Asset
from .media import analyze_reference, probe
from .duration import split_duration

router = APIRouter(prefix="/api/creative", tags=["creative"])
SKILLS = PROJECT_ROOT / "references" / "youtube-skills" / "youtube-agent-skill-main" / "skills"

class PlanRequest(BaseModel):
    text: str = Field(min_length=3, max_length=20000)
    language: Literal["Hindi", "English", "Hinglish"] = "Hindi"
    category: str = Field(default="Indian village stories", max_length=80)
    duration: int = Field(default=45, ge=45, le=120, multiple_of=5)
    shot_durations: list[Literal[5, 10]] = Field(default_factory=lambda: [5, 10], min_length=1, max_length=2)

class PlannedScene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    prompt: str = Field(min_length=10, max_length=4000)
    narration: str = Field(default="", max_length=1000)
    duration: Literal[5, 10] = 5
    camera: str = Field(default="Static", max_length=80)
    voice: str = Field(default="", max_length=100)
    sound_effect: str = Field(default="", max_length=300)

class PlanResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=180)
    hook: str = Field(min_length=1, max_length=1000)
    hooks: list[str] = Field(default_factory=list, max_length=5)
    scenes: list[PlannedScene] = Field(min_length=1, max_length=24)
    characters: list[dict] = Field(default_factory=list, max_length=12)
    youtube: dict = Field(default_factory=dict)

class HookRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    language: Literal["Hindi", "English", "Hinglish"] = "English"

class PackageRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    thumbnail: str = Field(default="", max_length=120)
    language: Literal["Hindi", "English", "Hinglish"] = "English"

@lru_cache(maxsize=2)
def utility(relative: str):
    path = SKILLS / relative
    if not path.is_file():
        raise HTTPException(503, "The audited YouTube skills archive is missing from references/.")
    spec = importlib.util.spec_from_file_location("storyorbit_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def hook_result(text: str, language: str) -> dict:
    if language == "Hindi" or re.search(r"[\u0900-\u097f]", text):
        return {"supported": False, "score": None, "verdict": None,
                "reason": "The supplied hook scorer uses English ASCII word patterns. Hindi scoring is unsupported.",
                "source": "youtube-agent-skill / yt-script"}
    module = utility("yt-script/hookscore.py")
    parts, score, formula, hits = module.score(text)
    return {"supported": True, "score": score, "verdict": score, "band": module.band(score),
            "properties": parts, "formula": formula, "source": "youtube-agent-skill / yt-script",
            "limitations": "English heuristic, not a prediction of views or retention." +
                          (" Hinglish results are approximate." if language == "Hinglish" else "")}

@router.post("/hook")
def score_hook(body: HookRequest):
    return hook_result(body.text, body.language)

@router.post("/package")
def lint_package(body: PackageRequest):
    if body.language == "Hindi" or re.search(r"[\u0900-\u097f]", body.title + body.thumbnail):
        return {"supported":False, "chars":len(body.title),
                "reason":"Title length is measured; English wording heuristics do not support Hindi."}
    result = utility("yt-package/title.py").check(body.title, body.thumbnail)
    result["supported"] = True
    result["limitations"] = "Editorial heuristic; display truncation depends on device and layout."
    if len(body.thumbnail.split()) > 3:
        result["issues"].append(("studio-thumbnail", "Keep thumbnail text to three words or fewer."))
    return result

def _llm(system: str, prompt: str) -> dict:
    config = read_settings(True)
    if not config.get("llm_configured"):
        raise HTTPException(409, "Configure an LLM endpoint and model in Settings to generate scripts. You can still write scenes manually.")
    base = validate_service_url(config["llm_base_url"])
    headers = {"Content-Type":"application/json"}
    if config.get("llm_api_key"):
        headers["Authorization"] = "Bearer " + config["llm_api_key"]
    try:
        with httpx.Client(timeout=180, follow_redirects=False, trust_env=False) as client:
            response = client.post(base + "/chat/completions", headers=headers, json={
                "model":config["llm_model"], "temperature":0.7,
                "response_format":{"type":"json_object"},
                "messages":[{"role":"system","content":system},{"role":"user","content":prompt}]})
        if response.status_code >= 400:
            raise HTTPException(502, f"Script provider returned HTTP {response.status_code}. Check the endpoint, model and account credits.")
        if len(response.content) > 1024 * 1024:
            raise ValueError("Script provider returned an oversized response.")
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Script provider did not return text.")
        content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content)
        return json.loads(content)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(502, "The script provider returned an invalid response: " + redact(exc)) from exc

@router.post("/plan")
def create_plan(body: PlanRequest):
    try:
        durations = split_duration(body.duration, body.shot_durations)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    hooks_file = SKILLS / "yt-script" / "hooks.json"
    formulas = []
    if hooks_file.exists():
        formulas = [{"name":h["name"],"shape":h["shape"]}
                    for h in json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]]
    instruction = """You are a cinematic short-film scriptwriter. Return ONLY a JSON object.
Produce an original, culturally authentic story from the supplied topic or script.
Preserve user-supplied story beats; do not imitate identifiable people from reference media.
Language requested applies to dialogue/narration; write precise visual generation prompts in English.
Each scene must be a single coherent shot. Follow the exact ordered shot durations supplied by the user message; their sum is the requested finished-video length.
Keep narration to at most 10 words for a 5-second scene or 20 words for a 10-second scene.
Maintain character appearance and clothing descriptions across every scene prompt.
Include framing, camera movement, lighting, action, facial expression, and environment.
No unsupported factual claims or invented sources. Mark fictional stories as fiction in the description.
Generate five distinct hooks using the supplied formulas. Start on a complete thought, add a turn,
deliver the promised payoff, finish on a clear line and suggest a loop when appropriate.
Sound effects are suggestions only; do not claim audio was created.
JSON shape: {"title":"...", "hook":"...", "hooks":["..."], "characters":[{"name":"...", "appearance":"...", "clothing":"...", "personality":"..."}],
"scenes":[{"prompt":"...", "narration":"...", "duration":5, "camera":"...", "sound_effect":"..."}],
"youtube":{"title":"...", "description":"...", "tags":["..."], "thumbnail":"three words maximum","thumbnail_brief":"..."}}.
No markdown, no publish action. Hook formulas: """ + json.dumps(formulas, ensure_ascii=False)
    raw = _llm(instruction, f"Language: {body.language}\nCategory: {body.category}\nTotal duration: {body.duration} seconds. Ordered shot durations: {durations}.\nTopic/script:\n{body.text}")
    try:
        result = PlanResult.model_validate(raw).model_dump()
    except ValueError as exc:
        raise HTTPException(502, "The LLM returned an invalid scene plan. Try again or choose another model.") from exc
    if [s["duration"] for s in result["scenes"]] != durations:
        raise HTTPException(502, f"The LLM did not follow the {body.duration}-second shot plan. Try again or edit the scenes manually.")
    for scene in result["scenes"]:
        scene["id"] = uuid.uuid4().hex
        scene["trim_end"] = scene["duration"]
    candidates = result.get("hooks") or [result["hook"]]
    ranked = [{"text":text, **hook_result(text, body.language)} for text in candidates]
    ranked.sort(key=lambda item: item.get("score") or 0, reverse=True)
    result["hook_analysis"] = ranked[:2]
    if ranked and ranked[0].get("supported"):
        result["hook"] = ranked[0]["text"]
    result["estimated_narration_seconds"] = round(sum(len(s["narration"].split()) for s in result["scenes"]) / 2.5, 1)
    result["source"] = "Configured LLM + MIT YouTube hook formulas; generated plan is editable."
    return result

def synthesize(text: str, output_path: Path, language: str = "Hindi", voice: str = "") -> Path:
    if not text.strip():
        raise ValueError("Narration text is empty.")
    if len(text) > 4000:
        raise ValueError("Narration must be at most 4000 characters per scene.")
    config = read_settings(True)
    if not config.get("tts_configured"):
        raise RuntimeError("Configure a compatible text-to-speech endpoint in Settings.")
    base = validate_service_url(config["tts_base_url"])
    headers = {}
    if config.get("tts_api_key"):
        headers["Authorization"] = "Bearer " + config["tts_api_key"]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".partial")
    try:
        with httpx.Client(timeout=180, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", base + "/audio/speech", headers=headers, json={
                    "model":config["tts_model"], "input":text,
                    "voice":voice or config.get("tts_voice","alloy"), "response_format":"mp3"}) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"Voice provider returned HTTP {response.status_code}.")
                count = 0
                with temporary.open("wb") as target:
                    for chunk in response.iter_bytes(65536):
                        count += len(chunk)
                        if count > 50 * 1024 * 1024:
                            raise RuntimeError("Voice response exceeds 50 MB.")
                        target.write(chunk)
        metadata = probe(temporary)
        if not metadata["has_audio"] or metadata["duration"] <= 0:
            raise RuntimeError("Voice provider did not return valid audio.")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path

@router.post("/analyze-reference/{asset_id}")
def reference_analysis(asset_id: str):
    with SessionLocal() as session:
        asset = session.get(Asset, asset_id)
        if not asset or asset.kind != "video":
            raise HTTPException(404, "Video asset not found.")
        try:
            result = analyze_reference(Path(asset.path))
            asset.meta = {**asset.meta, "reference_analysis":result}
            session.commit()
            return result
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, redact(exc)) from exc

