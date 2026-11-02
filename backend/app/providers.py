"""Real inference adapters. No demo, stock, or synthetic-video fallback exists."""
from __future__ import annotations

import base64
import copy
import json
import mimetypes
import secrets
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .config import PROJECT_ROOT, read_settings, validate_service_url
from .duration import split_duration

MAX_OUTPUT_BYTES = 1024 * 1024 * 1024

class ProviderError(RuntimeError):
    pass

class RemoteTerminal(ProviderError):
    """The remote provider has confirmed a terminal unsuccessful request."""

class SubmissionUnknown(ProviderError):
    """Network failed after submit might have reached the billed service."""


def checked_queue_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "queue.fal.run" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ProviderError("Provider returned an untrusted queue URL")
    return value


def checked_media_url(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or not (host == "fal.media" or host.endswith(".fal.media") or host == "storage.googleapis.com"):
        raise ProviderError("Provider returned an untrusted media URL")
    return value


def read_workflow(path: str) -> dict:
    target = Path(path).resolve()
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ProviderError("Workflow must be stored inside the project") from exc
    if not target.is_file() or target.stat().st_size > 5 * 1024 * 1024:
        raise ProviderError("Workflow file missing or too large")
    doc = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(doc.get("workflow"), dict) or not isinstance(doc.get("inputs"), dict) or not isinstance(doc.get("capabilities"), dict):
        raise ProviderError("Workflow JSON requires workflow, inputs, and capabilities objects; see workflow setup documentation")
    for name in ("prompt", "width", "height", "num_frames", "fps", "seed"):
        binding = doc["inputs"].get(name)
        if not isinstance(binding, list) or len(binding) != 2 or str(binding[0]) not in doc["workflow"] or binding[1] not in doc["workflow"][str(binding[0])].get("inputs", {}):
            raise ProviderError(f"Workflow requires a valid {name} node input mapping")
    caps = doc["capabilities"]
    for field in ("modes", "resolutions", "aspect_ratios", "durations", "fps"):
        if not isinstance(caps.get(field), list) or not caps[field]:
            raise ProviderError(f"Workflow capabilities require a nonempty {field} list")
    if set(caps["modes"]) - {"text", "image"}:
        raise ProviderError("Workflow supports only text/image modes")
    if "image" in caps["modes"] and not doc["inputs"].get("image"):
        raise ProviderError("Image workflows require an image input mapping")
    return doc


def provider_catalog(check_connection: bool = True) -> list[dict]:
    settings = read_settings(True)
    fal = {"id": "fal", "name": "Wan 2.2 · fal cloud", "configured": settings["fal_configured"], "available": settings["fal_configured"], "reason": "API key configured; account and credits are checked at submission." if settings["fal_configured"] else "Add a fal API key in Settings. Paid API credits required.", "modes": ["text", "image", "script"], "resolutions": ["480p", "720p"], "aspect_ratios": ["9:16", "16:9", "1:1"], "durations": [5, 10], "fps": [16], "cost_note": "Paid fal inference. Exact charge depends on the provider; billing is not estimated by this app.", "limitations": ["Character identity and natural motion are not guaranteed.", "Camera and style controls are prompt guidance.", "No native dialogue, lip-sync, or frame-accurate motion control.", "81 or 161 frames at 16fps yield approximately 5.06 or 10.06 seconds."]}
    comfy = {"id": "comfyui", "name": "ComfyUI · local workflow", "configured": False, "available": False, "reason": "Connect a ComfyUI server and a validated workflow manifest in Settings.", "modes": [], "resolutions": [], "aspect_ratios": [], "durations": [], "fps": [], "cost_note": "Uses your configured GPU/server; electricity or rented compute may cost money.", "limitations": ["Capabilities depend on the installed workflow and model weights.", "4 GB VRAM is below the recommended capacity for standard Wan 2.2 workflows.", "Running ComfyUI work cannot be safely interrupted on a shared server; cancellation stops this pipeline after the current request."]}
    if settings.get("comfyui_workflow_path"):
        try:
            doc = read_workflow(settings["comfyui_workflow_path"])
            comfy.update({k: doc["capabilities"][k] for k in ("modes", "resolutions", "aspect_ratios", "durations", "fps")})
            comfy["configured"] = True
            if "text" in comfy["modes"]:
                comfy["modes"] = [*comfy["modes"], "script"]
            if check_connection:
                base = validate_service_url(settings["comfyui_url"])
                response = httpx.get(base + "/system_stats", timeout=3, follow_redirects=False)
                response.raise_for_status()
            comfy["available"] = True
            comfy["reason"] = "Workflow configured; ComfyUI reachable." if check_connection else "Workflow configured."
        except Exception:
            comfy["reason"] = "ComfyUI is unavailable or workflow manifest is invalid. Check the server and workflow setup."
    return [fal, comfy]


def validate_request(project: dict) -> dict:
    provider = next(p for p in provider_catalog(False) if p["id"] == project["provider"])
    if not provider["configured"]:
        raise ProviderError(provider["reason"])
    config = project["settings"]
    for field, option in (("resolution", "resolutions"), ("aspect_ratio", "aspect_ratios"), ("fps", "fps")):
        if config[field] not in provider[option]:
            raise ProviderError(f"{field}={config[field]} is not supported by the selected provider")
    try:
        split_duration(config["duration"], provider["durations"])
    except ValueError as exc:
        raise ProviderError(str(exc)) from exc
    if project["mode"] not in provider["modes"]:
        raise ProviderError("This generation mode is not supported by the selected provider")
    if project["mode"] == "image" and not config.get("image_asset_id"):
        raise ProviderError("Image-to-video requires an uploaded reference image")
    for scene in project.get("scenes", []):
        if scene["duration"] not in provider["durations"]:
            raise ProviderError("A scene duration is unsupported by this provider")
    return provider


def composed_prompt(prompt: str, settings: dict) -> str:
    return f"{prompt}\nVisual style: {settings['style']}. Camera direction: {settings['camera']}. Original scene, natural movement, coherent spatial detail."

class FalProvider:
    def __init__(self, settings: dict | None = None):
        self.settings = settings or read_settings(True)
        self.headers = {"Authorization": "Key " + self.settings.get("fal_key", "")}

    def submit(self, prompt: str, options: dict, image: Path | None = None) -> dict:
        if options["duration"] not in (5, 10):
            raise ProviderError("fal requires individual 5/10-second shots; assemble them for a longer video.")
        payload = {"prompt": composed_prompt(prompt, options), "resolution": options["resolution"], "aspect_ratio": options["aspect_ratio"], "num_frames": options["duration"] * options["fps"] + 1, "frames_per_second": options["fps"], "interpolator_model": "none", "num_interpolated_frames": 0, "enable_safety_checker": True, "enable_output_safety_checker": True, "enable_prompt_expansion": False, "num_inference_steps": {"Fast": 15, "Balanced": 27, "High": 40}[options["quality"]]}
        if options.get("seed") is not None:
            payload["seed"] = options["seed"]
        mode = "image-to-video" if image else "text-to-video"
        if image:
            if image.stat().st_size > 20 * 1024 * 1024:
                raise ProviderError("Reference image exceeds 20 MB")
            mime = mimetypes.guess_type(image.name)[0] or "image/png"
            payload["image_url"] = f"data:{mime};base64," + base64.b64encode(image.read_bytes()).decode()
        try:
            response = httpx.post("https://queue.fal.run/fal-ai/wan/v2.2-a14b/" + mode, json=payload, headers=self.headers, timeout=60, follow_redirects=False)
        except httpx.RequestError as exc:
            raise SubmissionUnknown("The submission response was lost. It may have been billed; check the provider dashboard before retrying.") from exc
        if response.status_code >= 500:
            raise SubmissionUnknown("The provider returned a server error during submission. Check its dashboard before retrying to avoid a duplicate charge.")
        if response.status_code >= 400:
            raise ProviderError(f"fal rejected the request (HTTP {response.status_code}); check credentials, credits, prompt, and supported settings.")
        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("request_id"), str) or not data["request_id"]:
                raise ValueError("Missing request ID")
            return {"provider": "fal", "request_id": data["request_id"], **{key: checked_queue_url(data[key]) for key in ("status_url", "response_url", "cancel_url")}}
        except (ValueError, KeyError, TypeError, ProviderError) as exc:
            raise SubmissionUnknown("Provider accepted a request but returned an invalid receipt. Check its dashboard before retrying.") from exc

    def poll(self, handle: dict) -> dict:
        response = httpx.get(checked_queue_url(handle["status_url"]), params={"logs": 1}, headers=self.headers, timeout=30, follow_redirects=False)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise RemoteTerminal("Video inference failed: " + str(data["error"])[:700])
        return data

    def download(self, handle: dict, destination: Path) -> dict:
        response = httpx.get(checked_queue_url(handle["response_url"]), headers=self.headers, timeout=30, follow_redirects=False)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get("video"), dict) or not data["video"].get("url"):
            raise ProviderError("Provider did not return a video file")
        url = checked_media_url(data["video"]["url"])
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            for attempt in range(4):
                with client.stream("GET", url) as stream:
                    if stream.is_redirect:
                        url = checked_media_url(str(stream.url.join(stream.headers["location"])))
                        continue
                    stream.raise_for_status()
                    written = 0
                    with destination.open("wb") as output:
                        for chunk in stream.iter_bytes(1024 * 1024):
                            written += len(chunk)
                            if written > MAX_OUTPUT_BYTES:
                                raise ProviderError("Provider output exceeds 1 GB storage limit")
                            output.write(chunk)
                    return {"seed": data.get("seed"), "provider": "fal", "request_id": handle["request_id"]}
            raise ProviderError("Too many media download redirects")

    def cancel(self, handle: dict) -> str:
        response = httpx.put(checked_queue_url(handle["cancel_url"]), headers=self.headers, timeout=20, follow_redirects=False)
        if response.status_code == 200:
            return "Provider confirmed removal from its queue."
        if response.status_code == 202:
            return "Provider accepted cancellation; running inference may still finish and incur a charge."
        return "Local pipeline cancelled; provider cancellation was not confirmed and compute may still be charged."

class ComfyProvider:
    def __init__(self, settings: dict | None = None):
        self.settings = settings or read_settings(True)
        self.base = validate_service_url(self.settings["comfyui_url"])
        self.doc = read_workflow(self.settings["comfyui_workflow_path"])

    def submit(self, prompt: str, options: dict, image: Path | None = None) -> dict:
        if options["duration"] not in self.doc["capabilities"]["durations"]:
            raise ProviderError("Scene duration is unsupported by the selected workflow.")
        graph = copy.deepcopy(self.doc["workflow"])
        size = {"480p": 480, "720p": 720}[options["resolution"]]
        width, height = {"9:16": (size, round(size * 16 / 9 / 16) * 16), "16:9": (round(size * 16 / 9 / 16) * 16, size), "1:1": (size, size)}[options["aspect_ratio"]]
        dimensions = self.doc.get("dimensions", {}).get(options["resolution"], {}).get(options["aspect_ratio"])
        if dimensions:
            width, height = dimensions
        values = {"prompt": composed_prompt(prompt, options), "width": width, "height": height, "num_frames": options["duration"] * options["fps"] + 1, "fps": options["fps"], "seed": options.get("seed") if options.get("seed") is not None else secrets.randbelow(2147483647)}
        if image:
            with image.open("rb") as source:
                response = httpx.post(self.base + "/upload/image", files={"image": (image.name, source)}, data={"overwrite": "false"}, timeout=60, follow_redirects=False)
            response.raise_for_status()
            uploaded = response.json()
            values["image"] = (uploaded.get("subfolder", "").strip("/") + "/" + uploaded["name"]).lstrip("/")
        for key, value in values.items():
            binding = self.doc["inputs"].get(key)
            if binding:
                node, input_name = binding
                graph[str(node)]["inputs"][input_name] = value
        try:
            response = httpx.post(self.base + "/prompt", json={"prompt": graph, "client_id": "storyorbit-" + secrets.token_hex(8)}, timeout=60, follow_redirects=False)
        except httpx.RequestError as exc:
            raise SubmissionUnknown("ComfyUI submission status is unknown; inspect its queue before retrying.") from exc
        if response.status_code >= 500:
            raise SubmissionUnknown("ComfyUI server failed during submission; inspect its queue before retrying.")
        if response.status_code >= 400:
            raise ProviderError("ComfyUI rejected the workflow. Check required nodes, weights, and input bindings.")
        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("prompt_id"), str) or not data["prompt_id"]:
                raise ValueError("Missing prompt ID")
            return {"provider": "comfyui", "request_id": data["prompt_id"], "base_url": self.base}
        except (ValueError, KeyError, TypeError) as exc:
            raise SubmissionUnknown("ComfyUI accepted a request but returned an invalid receipt. Check its queue before retrying.") from exc

    def poll(self, handle: dict) -> dict:
        response = httpx.get(self.base + "/history/" + handle["request_id"], timeout=30, follow_redirects=False)
        response.raise_for_status()
        item = response.json().get(handle["request_id"])
        if item:
            status = item.get("status", {})
            if status.get("status_str") == "error":
                raise RemoteTerminal("ComfyUI workflow failed. Check model files, VRAM, and the ComfyUI server logs.")
            if status.get("completed") or item.get("outputs"):
                return {"status": "COMPLETED", "logs": []}
        queue = httpx.get(self.base + "/queue", timeout=15, follow_redirects=False)
        queue.raise_for_status()
        active = queue.json()
        running = any(len(entry) > 1 and entry[1] == handle["request_id"] for entry in active.get("queue_running", []))
        pending = any(len(entry) > 1 and entry[1] == handle["request_id"] for entry in active.get("queue_pending", []))
        if not running and not pending:
            raise ProviderError("ComfyUI no longer has this request in queue or history. Its history may have been cleared; manual retry required.")
        return {"status": "IN_PROGRESS" if running else "IN_QUEUE", "logs": []}

    def download(self, handle: dict, destination: Path) -> dict:
        response = httpx.get(self.base + "/history/" + handle["request_id"], timeout=30, follow_redirects=False)
        response.raise_for_status()
        item = response.json().get(handle["request_id"], {})
        files = []
        for node in item.get("outputs", {}).values():
            for field in ("videos", "gifs", "images"):
                files.extend(node.get(field, []))
        video = next((entry for entry in files if Path(entry.get("filename", "")).suffix.lower() in {".mp4", ".webm", ".mov"}), None)
        if not video:
            raise ProviderError("ComfyUI workflow produced no video. It must include a video-save/combine output node.")
        params = {key: video.get(key, "") for key in ("filename", "subfolder", "type")}
        with httpx.stream("GET", self.base + "/view", params=params, timeout=120, follow_redirects=False) as stream:
            stream.raise_for_status()
            written = 0
            with destination.open("wb") as output:
                for chunk in stream.iter_bytes(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_OUTPUT_BYTES:
                        raise ProviderError("ComfyUI output exceeds 1 GB storage limit")
                    output.write(chunk)
        return {"provider": "comfyui", "request_id": handle["request_id"]}

    def cancel(self, handle: dict) -> str:
        response = httpx.post(self.base + "/queue", json={"delete": [handle["request_id"]]}, timeout=20, follow_redirects=False)
        response.raise_for_status()
        return "Removed any pending ComfyUI entry. Running work is not interrupted on a shared server; this pipeline has stopped."


def get_provider(name: str):
    return FalProvider() if name == "fal" else ComfyProvider()
