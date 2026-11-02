"""Finished-video duration is separate from the model's short-shot duration."""
import copy

MIN_VIDEO_SECONDS = 45
MAX_VIDEO_SECONDS = 120


def upgrade_legacy_project(payload: dict) -> dict:
    result = copy.deepcopy(payload)
    settings = result.setdefault("settings", {})
    if settings.get("duration", 5) in (5, 10):
        settings["duration"] = MIN_VIDEO_SECONDS
    return result


def split_duration(total: int, supported: list[int]) -> list[int]:
    if not isinstance(total, int) or not MIN_VIDEO_SECONDS <= total <= MAX_VIDEO_SECONDS or total % 5:
        raise ValueError("Finished video length must be 45–120 seconds in 5-second steps.")
    shots = sorted({n for n in supported if n in (5, 10)}, reverse=True)
    choices = {0: []}
    for elapsed in range(1, total + 1):
        candidates = [choices[elapsed - shot] + [shot] for shot in shots if elapsed - shot in choices]
        if candidates:
            choices[elapsed] = min(candidates, key=len)
    if total not in choices:
        raise ValueError(f"The selected workflow cannot assemble exactly {total} seconds from its supported 5/10-second shots. Choose another length or workflow.")
    return sorted(choices[total], reverse=True)


def planned_duration(scenes: list[dict]) -> float:
    return sum(max(0, float(s.get("trim_end") or s["duration"]) - float(s.get("trim_start") or 0)) for s in scenes)


def require_timeline(scenes: list[dict], minimum: int):
    duration = planned_duration(scenes)
    if duration < minimum:
        raise ValueError(f"The storyboard contains {duration:g} seconds; add or lengthen scenes to reach at least {minimum} seconds before generating the complete video.")


def prompt_scenes(prompt: str, total: int, supported: list[int], make_id) -> list[dict]:
    durations = split_duration(total, supported)
    result = []
    for index, duration in enumerate(durations):
        beat = "Establish the setting and begin the action." if index == 0 else "Finish the action with a clear closing moment." if index == len(durations) - 1 else "Develop the action with a new visual beat."
        result.append({"id": make_id(), "prompt": f"{prompt}\nSequence shot {index + 1}/{len(durations)}. {beat} Keep the described subjects, clothing, location and lighting consistent.", "narration": "", "duration": duration, "trim_end": duration, "asset_id": None, "status": "draft"})
    return result
