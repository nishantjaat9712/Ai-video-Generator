from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse
from cryptography.fernet import Fernet

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("STORYORBIT_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
ASSET_DIR = DATA_DIR / "assets"
OUTPUT_DIR = DATA_DIR / "outputs"
ASSET_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
_LOCK = threading.RLock()
_SECRET_FIELDS = {"fal_key", "llm_api_key", "tts_api_key", "youtube_client_secret", "youtube_refresh_token"}
_DEFAULTS = {
    "comfyui_url": "http://127.0.0.1:8188",
    "comfyui_workflow_path": "",
    "llm_base_url": "https://api.openai.com/v1",
    "llm_model": "",
    "tts_base_url": "https://api.openai.com/v1",
    "tts_model": "tts-1",
    "tts_voice": "alloy",
    "youtube_enabled": False,
    "youtube_client_id": "",
    "youtube_redirect_uri": "http://127.0.0.1:8000/api/youtube/callback",
}
ENV_KEYS = {"fal_key": "FAL_KEY", "llm_api_key": "LLM_API_KEY", "tts_api_key": "TTS_API_KEY", "llm_base_url": "LLM_BASE_URL", "llm_model": "LLM_MODEL", "comfyui_url": "COMFYUI_URL", "comfyui_workflow_path": "COMFYUI_WORKFLOW_PATH"}


def _cipher() -> Fernet:
    key_path = DATA_DIR / ".credentials.key"
    if not key_path.exists():
        try:
            with key_path.open("xb") as target:
                target.write(Fernet.generate_key())
            key_path.chmod(0o600)
        except FileExistsError:
            pass
    return Fernet(key_path.read_bytes())


def validate_service_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Service URL must be an HTTP(S) base URL without credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote services require HTTPS; HTTP is allowed only on localhost")
    return value.rstrip("/")


def read_settings(include_secrets: bool = False) -> dict:
    with _LOCK:
        values = dict(_DEFAULTS)
        path = DATA_DIR / "settings.json"
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            for key, val in stored.items():
                if key not in set(_DEFAULTS) | _SECRET_FIELDS:
                    continue
                if key in _SECRET_FIELDS and val:
                    try:
                        values[key] = _cipher().decrypt(val.encode()).decode()
                    except Exception as exc:
                        raise RuntimeError("Stored credentials cannot be decrypted. Restore the matching credential key or reconfigure settings.") from exc
                else:
                    values[key] = val
        for field, env in ENV_KEYS.items():
            if os.getenv(env):
                values[field] = os.environ[env]
        values["fal_configured"] = bool(values.get("fal_key"))
        values["llm_configured"] = bool(values.get("llm_model") and (values.get("llm_api_key") or urlparse(values["llm_base_url"]).hostname in {"localhost", "127.0.0.1"}))
        values["tts_configured"] = bool(values.get("tts_model") and (values.get("tts_api_key") or urlparse(values["tts_base_url"]).hostname in {"localhost", "127.0.0.1"}))
        values["comfyui_configured"] = bool(values.get("comfyui_workflow_path"))
        if not include_secrets:
            values = {k: v for k, v in values.items() if k not in _SECRET_FIELDS}
        return values


def update_settings(changes: dict) -> dict:
    allowed = set(_DEFAULTS) | _SECRET_FIELDS
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    with _LOCK:
        values = read_settings(True)
        for key, value in changes.items():
            if key == "youtube_enabled":
                if not isinstance(value, bool):
                    raise ValueError("youtube_enabled must be a boolean")
            elif not isinstance(value, str) or len(value) > 8192:
                raise ValueError(f"Invalid setting: {key}")
            if key in {"llm_base_url", "tts_base_url", "comfyui_url"}:
                value = validate_service_url(value)
            if key == "comfyui_workflow_path" and value:
                path = Path(value).resolve()
                try:
                    path.relative_to(PROJECT_ROOT)
                except ValueError as exc:
                    raise ValueError("ComfyUI workflow must be stored inside the project directory") from exc
                if path.suffix.lower() != ".json" or not path.is_file():
                    raise ValueError("ComfyUI workflow must be an existing JSON file")
                value = str(path)
            values[key] = value
        stored = {k: v for k, v in values.items() if k in allowed}
        for key in _SECRET_FIELDS:
            if stored.get(key):
                stored[key] = _cipher().encrypt(stored[key].encode()).decode()
        temporary = DATA_DIR / "settings.json.tmp"
        temporary.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        temporary.replace(DATA_DIR / "settings.json")
        return read_settings()


def redact(message: object) -> str:
    text = str(message)
    try:
        settings = read_settings(True)
        for key in _SECRET_FIELDS:
            secret = settings.get(key)
            if secret:
                text = text.replace(secret, "[redacted]")
    except Exception:
        return "Provider or configuration error; inspect the local service configuration."
    return text[:2000]

