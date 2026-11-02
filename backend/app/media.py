"""Bounded FFmpeg processing. Every exported video is decoded and validated."""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def binary(name: str) -> str:
    configured = os.getenv(name.upper() + "_PATH")
    if configured and Path(configured).is_file():
        return configured
    candidates = sorted((ROOT / ".tools" / "ffmpeg").glob(f"**/{name}.exe"))
    if candidates:
        return str(candidates[0])
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"{name} is missing. Run scripts/Install.ps1 or set {name.upper()}_PATH.")

def run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Media processing failed: " + result.stderr[-2000:])
    return result

def probe(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ValueError("Media file does not exist.")
    raw = json.loads(run([binary("ffprobe"), "-v", "error", "-show_format",
                          "-show_streams", "-of", "json", str(path)], 60).stdout)
    videos = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
    video = videos[0] if videos else {}
    rate = video.get("avg_frame_rate", "0/1")
    try:
        fps = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        fps = 0
    duration = float(raw.get("format", {}).get("duration", 0) or 0)
    return {**raw, "width": video.get("width", 0), "height": video.get("height", 0),
            "duration": duration, "fps": fps, "has_audio": bool(audios),
            "video_codec": video.get("codec_name"), "size_bytes": path.stat().st_size}

def validate_video(path: str | Path) -> dict:
    metadata = probe(path)
    if metadata["width"] < 16 or metadata["height"] < 16 or not 0 < metadata["duration"] <= 3600:
        raise ValueError("Output has no valid video stream or its duration is outside the allowed range.")
    run([binary("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-f", "null", "-"], 600)
    return metadata

def srt_timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

def _dimensions(settings: dict) -> tuple[int, int]:
    short = {"480p": 480, "580p": 580, "720p": 720, "1080p": 1080}.get(settings.get("resolution", "720p"))
    if not short:
        raise ValueError("Unsupported render resolution.")
    ratio = settings.get("aspect_ratio", "9:16")
    long = round(short * 16 / 9 / 2) * 2
    if ratio == "9:16":
        return short, long
    if ratio == "16:9":
        return long, short
    if ratio == "1:1":
        return short, short
    raise ValueError("Unsupported aspect ratio.")

def assemble(clips: list[Path], output: Path, scenes: list[dict], settings: dict,
             narration_paths: list[Path | None] | None = None) -> dict:
    if not clips or len(clips) != len(scenes) or len(clips) > 30:
        raise ValueError("Assembly requires 1–30 clips and one scene per clip.")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < 1024 ** 3:
        raise RuntimeError("Keep at least 1 GB free for rendering.")
    width, height = _dimensions(settings)
    fps = int(settings.get("fps", 16))
    if fps not in (16, 24, 25, 30):
        raise ValueError("Unsupported render FPS.")
    volume = float(settings.get("audio_volume", 1))
    if not 0 <= volume <= 2:
        raise ValueError("Audio volume must be between 0 and 2.")
    narrations = narration_paths or [None] * len(clips)
    if len(narrations) != len(clips):
        raise ValueError("Narration count must match clips.")
    minimum = float(settings.get("minimum_duration", 0))
    clip_infos = [probe(clip) for clip in clips]
    available = sum(min(float(scene.get("trim_end") or info["duration"]), info["duration"]) - float(scene.get("trim_start") or 0) for scene, info in zip(scenes, clip_infos))
    if available < minimum:
        raise ValueError(f"The trimmed clips contain {available:g} seconds. Add footage to reach at least {minimum:g} seconds; clips are not looped or slowed down.")
    with tempfile.TemporaryDirectory(prefix="render-", dir=output.parent) as folder:
        workspace = Path(folder)
        normalized, captions, cursor = [], [], 0.0
        for index, (clip, scene, narration) in enumerate(zip(clips, scenes, narrations)):
            info = clip_infos[index]
            start = float(scene.get("trim_start") or 0)
            end = float(scene.get("trim_end") or info["duration"])
            if not 0 <= start < end <= info["duration"] + 0.15:
                raise ValueError(f"Scene {index + 1}: trim times must fit the generated clip.")
            duration = min(end, info["duration"]) - start
            target = workspace / f"scene-{index:03d}.mp4"
            args = [binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(start), "-t", str(duration), "-i", str(clip)]
            if narration:
                voice_info = probe(narration)
                if voice_info["duration"] > duration + .15:
                    raise ValueError(f"Scene {index + 1}: narration is {voice_info['duration']:.1f}s but the clip is {duration:.1f}s. Shorten dialogue or lengthen the scene.")
                args += ["-i", str(narration)]
                audio = "1:a:0"
            elif info["has_audio"]:
                audio = "0:a:0"
            else:
                args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                audio = "1:a:0"
            filters = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
            audio_filters = f"volume={volume},apad"
            if settings.get("noise_reduction"):
                audio_filters = "afftdn," + audio_filters
            args += ["-map", "0:v:0", "-map", audio, "-vf", filters, "-af", audio_filters,
                     "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-c:a", "aac",
                     "-ar", "48000", "-ac", "2", "-t", str(duration),
                     "-movflags", "+faststart", str(target)]
            run(args)
            normalized.append(target)
            caption = str(scene.get("narration") or scene.get("caption") or "").strip()
            if caption:
                # Smaller word groups avoid a full script covering the screen.
                words = caption.split()
                groups = [" ".join(words[i:i+10]) for i in range(0, len(words), 10)]
                for num, group in enumerate(groups):
                    a = cursor + duration * num / len(groups)
                    b = cursor + duration * (num + 1) / len(groups)
                    captions.append(f"{len(captions)+1}\n{srt_timestamp(a)} --> {srt_timestamp(b)}\n{group}\n")
            cursor += duration
        listing = workspace / "concat.txt"
        # Generated basenames only; no user path enters concat syntax.
        listing.write_text("".join(f"file '{p.name}'\n" for p in normalized), encoding="utf-8")
        joined = workspace / "joined.mp4"
        run([binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
             "-safe", "1", "-i", str(listing), "-c", "copy", str(joined)])
        music_path = settings.get("music_path")
        if music_path:
            music_info = probe(Path(music_path))
            if not music_info["has_audio"]:
                raise ValueError("Background music has no valid audio stream.")
            music_volume = float(settings.get("music_volume", 0.15))
            if not 0 <= music_volume <= 1:
                raise ValueError("Music volume must be between 0 and 1.")
            mixed = workspace / "mixed.mp4"
            run([binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(joined), "-stream_loop", "-1", "-i", str(music_path),
                 "-filter_complex", f"[1:a]volume={music_volume}[music];[0:a][music]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
                 "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                 "-t", str(cursor), "-movflags", "+faststart", str(mixed)])
            joined = mixed
        subtitle_path = None
        if settings.get("subtitles") and captions:
            subtitle_path = output.with_suffix(".srt")
            subtitle_path.write_text("\n".join(captions), encoding="utf-8")
            language = "hin" if settings.get("language") == "Hindi" else "eng"
            run([binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(joined),
                 "-i", str(subtitle_path), "-map", "0:v", "-map", "0:a", "-map", "1:0",
                 "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text", "-metadata:s:s:0",
                 f"language={language}", "-disposition:s:0", "default",
                 "-movflags", "+faststart", str(output)])
        else:
            shutil.copyfile(joined, output)
        result = validate_video(output)
        video_stream = next(s for s in result["streams"] if s.get("codec_type") == "video")
        video_seconds = float(video_stream.get("duration") or result["duration"])
        if video_seconds + 0.001 < minimum:
            raise ValueError(f"Rendered video is {video_seconds:g} seconds, below the {minimum:g}-second minimum.")
        result["subtitle_path"] = str(subtitle_path) if subtitle_path else None
        result["caption_timing"] = "Scene-aligned, evenly spaced word groups; not forced alignment."
        result["lip_sync"] = False
        return result

def analyze_reference(path: Path) -> dict:
    metadata = probe(path)
    if not metadata["width"] or metadata["duration"] > 120:
        raise ValueError("Reference analysis supports video clips up to 120 seconds.")
    with tempfile.TemporaryDirectory(prefix="analysis-", dir=path.parent) as folder:
        pattern = Path(folder) / "%02d.jpg"
        run([binary("ffmpeg"), "-v", "error", "-y", "-i", str(path), "-vf",
             "fps=1/2,scale=160:-1", "-frames:v", "60", str(pattern)], 120)
        from PIL import Image, ImageStat, ImageChops
        frames = []
        for item in sorted(Path(folder).glob("*.jpg")):
            with Image.open(item) as source:
                frames.append(source.convert("RGB").copy())
        means = [ImageStat.Stat(frame).mean for frame in frames]
        brightness = round(sum(sum(m)/3 for m in means) / len(means), 1) if means else None
        differences = [sum(ImageStat.Stat(ImageChops.difference(a,b)).mean)/3
                       for a,b in zip(frames,frames[1:])]
    return {"width":metadata["width"], "height":metadata["height"],
            "duration":metadata["duration"], "fps":round(metadata["fps"],3),
            "aspect_ratio": f"{metadata['width']}:{metadata['height']}",
            "has_audio":metadata["has_audio"], "mean_brightness_0_255":brightness,
            "frame_change_mean":round(sum(differences)/len(differences),2) if differences else None,
            "analysis_type":"Measured technical properties",
            "limitations":["Frame differences combine motion, lighting and cuts; they do not identify camera movement.",
                            "Automatic semantic style transfer, identity recognition and facial-expression analysis are not configured."],
            "suggestion":"Describe the lighting, framing and pacing in your new scene prompt; keep the story and characters original."}

