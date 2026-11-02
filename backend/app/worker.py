"""Durable single-concurrency worker. Run separately: python -m app.worker.

SQLite atomic claims or PostgreSQL row locks prevent duplicate local execution.
Remote submission markers are committed before network calls. A crash in the
submission-response gap becomes needs_review instead of silently paying twice.
"""
from __future__ import annotations
import copy, json, os, signal, threading, time
from pathlib import Path
import httpx
from sqlalchemy import select, update, text
from .config import OUTPUT_DIR, DATA_DIR, read_settings, redact
from .db import SessionLocal, init_db, DATABASE_URL
from .models import Project, Asset, Character, Job, Usage, WorkerHeartbeat, uid, now
from .providers import get_provider, validate_request, ProviderError, SubmissionUnknown, RemoteTerminal
from .duration import prompt_scenes, require_timeline, planned_duration
from .media import probe, validate_video, assemble
from .creative import synthesize, create_plan, PlanRequest

LEASE_SECONDS = 90
POLL_SECONDS = float(os.getenv("STORYORBIT_POLL_SECONDS","3"))
MAX_POLL_SECONDS = float(os.getenv("STORYORBIT_MAX_POLL_SECONDS","7200"))
WORKER_ID = uid()
_STOP = threading.Event()

class Cancelled(RuntimeError):
    pass

def _lock(session):
    if DATABASE_URL.startswith("sqlite"):
        session.execute(text("BEGIN IMMEDIATE"))

def touch(worker_id: str, job_id: str | None = None):
    with SessionLocal() as session:
        row=session.get(WorkerHeartbeat,worker_id)
        if row: row.last_seen=time.time()
        else: session.add(WorkerHeartbeat(id=worker_id,last_seen=time.time()))
        if job_id:
            session.execute(update(Job).where(Job.id==job_id,Job.worker_id==worker_id,Job.status=="running").values(lease_until=time.time()+LEASE_SECONDS))
        session.commit()

def recover_jobs():
    with SessionLocal() as session:
        _lock(session)
        expired=list(session.scalars(select(Job).where(Job.status=="running",Job.lease_until<time.time()).with_for_update()))
        for row in expired:
            payload=row.payload
            unknown=any(scene not in payload.get("handles",{}) for scene in payload.get("submitted",{}))
            row.status="needs_review" if unknown else "cancelled" if row.cancel_requested else "queued"
            row.stage=row.status
            row.error="Worker stopped during provider submission; inspect the provider dashboard before retrying." if unknown else None
            row.worker_id=None;row.lease_until=None;row.updated_at=now()
            row.logs=[*row.logs,{"time":now(),"message":"Expired worker lease recovered. "+("Uncertain submission requires explicit review." if unknown else "Saved assets and provider handles will be resumed.")}][-200:]
        session.commit()

def claim_job(worker_id: str) -> str | None:
    with SessionLocal() as session:
        _lock(session)
        if not DATABASE_URL.startswith("sqlite"):
            # One local pipeline across workers sharing this DB, protecting GPU VRAM.
            session.execute(text("SELECT pg_advisory_xact_lock(731081)"))
        if session.scalar(select(Job.id).where(Job.status=="running").limit(1)):
            return None
        query=select(Job).where(Job.status=="queued",Job.cancel_requested==False).order_by(Job.created_at).limit(1)
        if not DATABASE_URL.startswith("sqlite"):
            query=query.with_for_update(skip_locked=True)
        row=session.scalar(query)
        if not row:return None
        row.status="running";row.stage="preparing";row.worker_id=worker_id
        row.lease_until=time.time()+LEASE_SECONDS;row.updated_at=now()
        session.commit();return row.id

def snapshot(job_id: str) -> dict:
    with SessionLocal() as session:
        row=session.get(Job,job_id)
        if not row: raise RuntimeError("Job was removed.")
        if row.cancel_requested or _STOP.is_set():raise Cancelled("Pipeline cancellation requested.")
        return copy.deepcopy(row.payload)

def save_payload(job_id: str,payload: dict,project_update: bool=False):
    with SessionLocal() as session:
        row=session.get(Job,job_id)
        if not row:raise RuntimeError("Job was removed.")
        row.payload=copy.deepcopy(payload);row.updated_at=now()
        if project_update:
            project=session.get(Project,row.project_id)
            if not project:raise RuntimeError("Project was removed.")
            project.payload=copy.deepcopy(payload["project"]);project.updated_at=now()
        session.commit()

def stage(job_id: str,name: str,progress: int,message: str | None=None):
    with SessionLocal() as session:
        row=session.get(Job,job_id)
        row.stage=name;row.progress=progress;row.updated_at=now()
        if message:
            entry={"time":now(),"message":redact(message)}
            if not row.logs or row.logs[-1].get("message")!=entry["message"]:
                row.logs=[*row.logs,entry][-200:]
        session.commit()

def asset_path(asset_id: str,kind: str="video") -> Path:
    with SessionLocal() as session:
        asset=session.get(Asset,asset_id)
        if not asset or asset.kind!=kind:raise ValueError(f"Referenced {kind} asset is missing.")
        path=Path(asset.path).resolve()
        try:path.relative_to(DATA_DIR)
        except ValueError as exc:raise ValueError("Asset is outside the storage directory.") from exc
        if not path.is_file():raise ValueError("Asset file is missing.")
        return path

def record_asset(path: Path,name: str,metadata: dict) -> str:
    with SessionLocal() as session:
        asset=Asset(path=str(path),name=name,kind="video",meta=metadata)
        session.add(asset);session.commit();return asset.id

def record_usage(job_id: str,handle: dict,duration: int):
    with SessionLocal() as session:
        if not session.scalar(select(Usage.id).where(Usage.job_id==job_id,Usage.request_id==handle["request_id"])):
            session.add(Usage(job_id=job_id,provider=handle["provider"],request_id=handle["request_id"],duration_seconds=duration,cost_usd=None,details={"billing":"Provider dashboard is authoritative."}))
            session.commit()

def prompt_with_characters(project: dict,prompt: str) -> str:
    descriptions=[]
    with SessionLocal() as session:
        for character_id in project["settings"].get("character_ids",[]):
            row=session.get(Character,character_id)
            if row:
                value=row.payload
                descriptions.append(f"{value['name']}: {value.get('appearance','')}; clothing: {value.get('clothing','')}; personality: {value.get('personality','')}.")
    if descriptions:
        prompt+="\nRecurring character descriptions (identity consistency is not guaranteed):\n"+"\n".join(descriptions)
    return prompt

def _cancel_provider(payload: dict):
    message="Local pipeline cancelled."
    for scene_id,handle in payload.get("handles",{}).items():
        try:
            provider=get_provider(handle["provider"])
            # Never send a request to a changed ComfyUI host when resuming a handle.
            if handle["provider"]=="comfyui" and provider.base!=handle.get("base_url"):
                continue
            message=provider.cancel(handle)
            if handle["provider"]=="fal" and message.startswith("Provider confirmed removal"):
                payload.setdefault("terminal_handles",[]).append(scene_id)
        except Exception:
            message="Local pipeline cancelled; remote cancellation was not confirmed. Check the provider dashboard."
    return message

def _wait_inference(job_id: str,provider,handle: dict,progress: int):
    failures=0
    started=time.monotonic()
    while True:
        snapshot(job_id)
        if time.monotonic()-started > MAX_POLL_SECONDS:
            raise RuntimeError("Provider wait timed out. Its request is retained; Retry resumes polling without paying for a new submission.")
        try:
            result=provider.poll(handle)
            failures=0
        except httpx.HTTPError as exc:
            failures+=1
            if failures>=4:
                raise RuntimeError("Provider status could not be reached. The saved request can be resumed with Retry.") from exc
            stage(job_id,"video generation",progress,f"Status connection interrupted; retry {failures}/3. No new generation is submitted.")
            time.sleep(min(2**failures,10))
            continue
        state=result.get("status")
        if state=="COMPLETED":return
        if state in {"FAILED","ERROR","CANCELLED"}:
            raise RemoteTerminal("Provider generation ended with status "+str(state)+".")
        if state not in {"IN_PROGRESS","IN_QUEUE","QUEUED"}:
            raise ProviderError("Provider returned an unknown generation status.")
        message="Provider is generating the scene." if state=="IN_PROGRESS" else "Waiting in the provider queue."
        stage(job_id,"video generation",progress,message)
        # Stage completion is measured; no elapsed-time percentage is invented.
        _STOP.wait(POLL_SECONDS)

def execute_job(job_id: str):
    payload=snapshot(job_id)
    project=payload["project"];settings=project["settings"]
    folder=OUTPUT_DIR/job_id;folder.mkdir(parents=True,exist_ok=True)
    capabilities = validate_request(project) if not payload.get("render_only") else None
    if not project.get("scenes"):
        stage(job_id,"scene planning",5,"Preparing scene instructions.")
        if project["mode"]=="script":
            plan=create_plan(PlanRequest(text=project["prompt"],language=project["language"],duration=settings["duration"],shot_durations=[n for n in capabilities["durations"] if n in (5,10)]))
            project["scenes"]=plan["scenes"]
            project["metadata"]={**project.get("metadata",{}),"plan":plan}
        else:
            project["scenes"]=prompt_scenes(project["prompt"],settings["duration"],capabilities["durations"],uid)
        save_payload(job_id,payload,True)
    scenes=project["scenes"]
    target_scene=payload.get("scene_id")
    if not target_scene and not payload.get("render_only"):
        require_timeline(scenes, settings["duration"])
    chosen=[s for s in scenes if not target_scene or s["id"]==target_scene]
    if target_scene and not chosen:raise ValueError("Selected scene is missing.")
    if settings.get("reference_asset_id"):
        stage(job_id,"scene planning",8,"Reference video is retained for your style analysis. Its content is not automatically copied or used as an inference input.")
    provider=None
    if not payload.get("render_only"):
        provider=get_provider(project["provider"])
    for index,scene in enumerate(chosen):
        snapshot(job_id)
        progress=10+int(60*index/len(chosen))
        if scene.get("asset_id"):
            validate_video(asset_path(scene["asset_id"]))
            continue
        if payload.get("render_only"):raise ValueError("Every scene needs a generated or imported video before rendering.")
        scene_id=scene["id"]
        options={**settings,"duration":scene["duration"],"camera":scene.get("camera") or settings["camera"]}
        image=asset_path(settings["image_asset_id"],"image") if project["mode"]=="image" and settings.get("image_asset_id") else None
        handle=payload.setdefault("handles",{}).get(scene_id)
        if handle and handle["provider"]=="comfyui" and provider.base!=handle.get("base_url"):
            raise RuntimeError("ComfyUI URL changed since submission. Restore the original URL to resume this job.")
        if not handle:
            if scene_id in payload.setdefault("submitted",{}):
                raise SubmissionUnknown("A prior submission may have reached the provider. Check its dashboard before retrying.")
            payload["submitted"][scene_id]=now()
            save_payload(job_id,payload)
            stage(job_id,"video generation",progress,f"Submitting scene {index+1}/{len(chosen)} to the configured provider.")
            handle=provider.submit(prompt_with_characters(project,scene["prompt"]),options,image)
            payload["handles"][scene_id]=handle
            save_payload(job_id,payload)
            record_usage(job_id,handle,scene["duration"])
        try:
            _wait_inference(job_id,provider,handle,progress)
        except RemoteTerminal:
            payload.setdefault("terminal_handles", []).append(scene_id)
            save_payload(job_id,payload)
            raise
        snapshot(job_id)
        destination=folder/(scene_id+".mp4")
        partial=destination.with_suffix(".partial")
        stage(job_id,"output validation",progress,f"Downloading and validating scene {index+1}.")
        try:
            details=provider.download(handle,partial)
            metadata=validate_video(partial)
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
        scene["asset_id"]=record_asset(destination,f"scene-{index+1}.mp4",{**metadata,**details,"source":"model-inference"})
        scene["status"]="generated"
        save_payload(job_id,payload,True)
    snapshot(job_id)
    if target_scene and (any(not s.get("asset_id") for s in scenes) or planned_duration(scenes) < settings["duration"]):
        stage(job_id,"scene complete",100,"Selected scene is generated. Complete a storyboard of at least the selected video length before assembling.")
        with SessionLocal() as session:
            row=session.get(Job,job_id);row.status="completed";row.output_asset_id=chosen[0]["asset_id"];row.lease_until=None;row.updated_at=now();session.commit()
        return
    paths=[asset_path(s["asset_id"]) for s in scenes]
    narrations=[]
    for index,scene in enumerate(scenes):
        snapshot(job_id)
        if settings.get("narration_enabled") and scene.get("narration","").strip():
            stage(job_id,"voice generation",72+int(8*index/len(scenes)),f"Creating narration for scene {index+1}.")
            voice=folder/(scene["id"]+"-narration.mp3")
            # Existing validated narration survives interruption.
            if not voice.exists():
                synthesize(scene["narration"],voice,project["language"],scene.get("voice") or settings.get("voice",""))
            elif not probe(voice)["has_audio"]:
                raise ValueError("Saved narration is invalid; remove the failed narration file and retry.")
            narrations.append(voice)
        else:narrations.append(None)
    render_settings={**settings,"language":project["language"],"minimum_duration":settings["duration"]}
    if settings.get("music_asset_id"):
        render_settings["music_path"]=str(asset_path(settings["music_asset_id"],"audio"))
    stage(job_id,"rendering",85,"Normalizing, trimming and assembling validated scene clips.")
    destination=folder/"final.mp4"
    metadata=assemble(paths,destination,scenes,render_settings,narrations)
    snapshot(job_id)
    stage(job_id,"export validation",95,"Validating the completed MP4 export.")
    validate_video(destination)
    with SessionLocal() as session:
        sources=[{"asset_id":scene["asset_id"],"source":session.get(Asset,scene["asset_id"]).meta.get("source","user-upload")} for scene in scenes]
    output_id=record_asset(destination,project["title"][:100]+".mp4",{**metadata,"source":"assembled-scenes","scene_sources":sources})
    project["output_asset_id"]=output_id
    save_payload(job_id,payload,True)
    with SessionLocal() as session:
        row=session.get(Job,job_id)
        row.status="completed";row.stage="export complete";row.progress=100;row.output_asset_id=output_id
        row.lease_until=None;row.updated_at=now()
        row.logs=[*row.logs,{"time":now(),"message":"MP4 export validated and saved. It is ready to preview and download."}][-200:]
        session.commit()

def process_job(job_id: str,worker_id: str=WORKER_ID):
    stop_heartbeat=threading.Event()
    def keepalive():
        while not stop_heartbeat.wait(10):
            try:touch(worker_id,job_id)
            except Exception:pass
    thread=threading.Thread(target=keepalive,daemon=True)
    thread.start()
    try:
        touch(worker_id,job_id)
        execute_job(job_id)
    except Exception as exc:
        with SessionLocal() as session:
            row=session.get(Job,job_id)
            if not row:return
            payload=copy.deepcopy(row.payload)
        if isinstance(exc,Cancelled):
            message=_cancel_provider(payload)
            status="cancelled"
        elif isinstance(exc,SubmissionUnknown):
            message=redact(exc);status="needs_review"
        else:
            message=redact(exc);status="failed"
            # A failed submit with no ambiguity (e.g. 401/422) can be retried explicitly.
            for scene in list(payload.get("submitted",{})):
                if scene not in payload.get("handles",{}):
                    payload["submitted"].pop(scene,None)
        with SessionLocal() as session:
            row=session.get(Job,job_id)
            row.status=status;row.stage=status;row.error=message if status!="cancelled" else None
            row.payload=payload;row.lease_until=None;row.updated_at=now()
            row.logs=[*row.logs,{"time":now(),"message":message}][-200:]
            session.commit()
    finally:
        stop_heartbeat.set();thread.join(timeout=2)

def main():
    # Register optional publishing tables before schema creation.
    try:
        from . import youtube
    except ModuleNotFoundError as exc:
        if exc.name not in {"app.youtube",__package__+".youtube"}:raise
        youtube=None
    init_db()
    signal.signal(signal.SIGINT,lambda *_:_STOP.set())
    if hasattr(signal,"SIGTERM"):signal.signal(signal.SIGTERM,lambda *_:_STOP.set())
    print("StoryOrbit worker started. One active pipeline per shared database.",flush=True)
    while not _STOP.is_set():
        touch(WORKER_ID)
        recover_jobs()
        job_id=claim_job(WORKER_ID)
        if job_id:
            process_job(job_id)
        else:
            if youtube is not None:
                try:youtube.process_next_publish()
                except Exception as exc:print("Publishing worker error: "+redact(exc),flush=True)
            _STOP.wait(2)
    print("StoryOrbit worker stopped.",flush=True)

if __name__=="__main__":
    main()

