"""StoryOrbit local API. Actual generation runs in the separate durable worker."""
from __future__ import annotations
import copy, hmac, io, json, os, re, shutil, subprocess, tempfile, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import psutil
from PIL import Image, UnidentifiedImageError
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Body
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, update, delete, text
from pydantic import ValidationError
from .config import DATA_DIR, ASSET_DIR, PROJECT_ROOT, read_settings, update_settings, redact
from .db import SessionLocal, init_db, DATABASE_URL
from .models import Project, Asset, Character, Job, Usage, WorkerHeartbeat, uid, now
from .schemas import ProjectInput, CharacterInput, GenerateInput, RetryInput
from .providers import provider_catalog, validate_request, ProviderError
from .media import probe, validate_video, binary
from .duration import upgrade_legacy_project, require_timeline

ACTIVE = ("queued", "running")
TERMINAL = ("completed", "failed", "cancelled", "needs_review")
MAX_UPLOAD = 100 * 1024 * 1024
RATE_LIMIT = int(os.getenv("STORYORBIT_RATE_LIMIT", "120"))
MAX_QUEUED = int(os.getenv("STORYORBIT_MAX_QUEUED", "8"))
DAILY_QUOTA = int(os.getenv("STORYORBIT_DAILY_QUOTA", "40"))
_buckets: dict[str, deque] = defaultdict(deque)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("STORYORBIT_PUBLIC") == "1" and not os.getenv("STORYORBIT_API_TOKEN"):
        raise RuntimeError("Public mode requires STORYORBIT_API_TOKEN and an authenticated reverse proxy.")
    init_db()
    yield

app = FastAPI(title="StoryOrbit AI Studio", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv("STORYORBIT_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(","))

class BoundedRequestBody:
    """Bound incoming bodies before multipart/JSON parsing, including chunked requests."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"} or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        limit = MAX_UPLOAD + 1024 * 1024 if scope["path"] == "/api/assets" else 2 * 1024 * 1024
        length = headers.get(b"content-length")
        try:
            declared = int(length) if length is not None else None
            if declared is not None and declared < 0:
                raise ValueError()
        except (ValueError, TypeError):
            return await JSONResponse({"detail":"Invalid content length."},400)(scope,receive,send)
        if declared is not None and declared > limit:
            return await JSONResponse({"detail":"Request is too large."},413)(scope,receive,send)
        with tempfile.SpooledTemporaryFile(max_size=1024*1024,dir=DATA_DIR) as body:
            total=0
            while True:
                message=await receive()
                if message["type"]=="http.disconnect":
                    return
                chunk=message.get("body",b"")
                total+=len(chunk)
                if total>limit:
                    return await JSONResponse({"detail":"Request is too large."},413)(scope,receive,send)
                body.write(chunk)
                if not message.get("more_body",False):
                    break
            if declared is not None and total != declared:
                return await JSONResponse({"detail":"Content length does not match the request body."},400)(scope,receive,send)
            body.seek(0)
            finished=False
            async def replay():
                nonlocal finished
                if finished:
                    return await receive()
                chunk=body.read(65536)
                more=body.tell()<total
                finished=not more
                return {"type":"http.request","body":chunk,"more_body":more}
            await self.app(scope,replay,send)

@app.middleware("http")
async def protect(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        allowed = set(os.getenv("STORYORBIT_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000,http://127.0.0.1:8000").split(","))
        if origin and origin not in allowed:
            return JSONResponse({"detail":"Request origin is not allowed."}, 403)
        token = os.getenv("STORYORBIT_API_TOKEN")
        callback = request.url.path == "/api/youtube/callback"
        if token and not callback:
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail":"Authentication required."}, 401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.url.path != "/api/assets" and not request.headers.get("content-type", "").startswith("application/json"):
                # Empty cancel/render requests are valid.
                if request.headers.get("content-length", "0") not in {"0",""}:
                    return JSONResponse({"detail":"JSON content type is required."}, 415)
            limit = MAX_UPLOAD + 1024 * 1024 if request.url.path == "/api/assets" else 2 * 1024 * 1024
            try:
                content_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail":"Invalid content length."}, 400)
            if content_length > limit:
                return JSONResponse({"detail":"Request is too large."}, 413)
            client = request.client.host if request.client else "local"
            queue = _buckets[client]
            instant = time.monotonic()
            while queue and instant - queue[0] > 60:
                queue.popleft()
            if len(queue) >= RATE_LIMIT:
                return JSONResponse({"detail":"Too many changes; wait a minute and retry."}, 429, headers={"Retry-After":"60"})
            queue.append(instant)
    try:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"detail":redact(exc)}, 422)

app.add_middleware(BoundedRequestBody)

def project_json(row: Project) -> dict:
    return {**upgrade_legacy_project(row.payload), "id":row.id, "created_at":row.created_at, "updated_at":row.updated_at}

def job_json(row: Job) -> dict:
    return {key:getattr(row,key) for key in ("id","project_id","status","stage","progress","error","logs","cancel_requested","output_asset_id","created_at","updated_at")}

def asset_json(row: Asset) -> dict:
    return {"id":row.id,"url":f"/api/assets/{row.id}/file","name":row.name,"kind":row.kind,"metadata":row.meta,"created_at":row.created_at}

def storage_path(path: str) -> Path:
    target = Path(path).resolve()
    try:
        target.relative_to(DATA_DIR)
    except ValueError as exc:
        raise HTTPException(404,"Asset not found.") from exc
    if not target.is_file():
        raise HTTPException(404,"Asset file is missing from storage.")
    return target

def validate_refs(session, payload: dict):
    settings = payload["settings"]
    refs = [(settings.get("image_asset_id"),"image"), (settings.get("reference_asset_id"),"video"),
            (settings.get("music_asset_id"),"audio")]
    refs += [(s.get("asset_id"),"video") for s in payload.get("scenes",[])]
    for asset_id,kind in refs:
        if asset_id:
            asset = session.get(Asset,asset_id)
            if not asset or asset.kind != kind:
                raise HTTPException(422,f"Referenced {kind} asset does not exist.")
            storage_path(asset.path)
    for character_id in settings.get("character_ids",[]):
        if not session.get(Character,character_id):
            raise HTTPException(422,"Selected character does not exist.")

def transaction_lock(session):
    if DATABASE_URL.startswith("sqlite"):
        session.execute(text("BEGIN IMMEDIATE"))

def get_project(session, project_id: str, lock: bool = False):
    row = session.scalar(select(Project).where(Project.id==project_id).with_for_update()) if lock else session.get(Project,project_id)
    if not row:
        raise HTTPException(404,"Project not found.")
    return row

def idle_project(session, project_id: str):
    if session.scalar(select(Job.id).where(Job.project_id==project_id,Job.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409,"Wait for or cancel the active project job before editing.")

@app.get("/api/health")
def health():
    with SessionLocal() as session:
        session.execute(text("SELECT 1"))
        seen = session.scalar(select(WorkerHeartbeat.last_seen).order_by(WorkerHeartbeat.last_seen.desc()).limit(1))
    try:
        binary("ffmpeg"); binary("ffprobe")
        ffmpeg = True
    except RuntimeError:
        ffmpeg = False
    return {"status":"ok","database":"ok","worker":{"alive":bool(seen and time.time()-seen < 45),
            "last_seen":datetime.fromtimestamp(seen,timezone.utc).isoformat() if seen else None},
            "ffmpeg_available":ffmpeg}

@app.get("/api/hardware")
def hardware():
    ram = psutil.virtual_memory()
    gpus = []
    try:
        result = subprocess.run(["nvidia-smi","--query-gpu=name,memory.total,memory.free,driver_version","--format=csv,noheader,nounits"],
                                capture_output=True,text=True,timeout=4,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if result.returncode==0:
            for line in result.stdout.strip().splitlines():
                name,total,free,driver = [part.strip() for part in line.split(",")]
                gpus.append({"name":name,"memory_total_mb":float(total),"memory_free_mb":float(free),"driver":driver})
    except (OSError,ValueError,subprocess.TimeoutExpired):
        pass
    return {"gpus":gpus,"ram_total_gb":round(ram.total/1024**3,1),"ram_available_gb":round(ram.available/1024**3,1),
            "disk_free_gb":round(shutil.disk_usage(DATA_DIR).free/1024**3,1),"cpu_percent":psutil.cpu_percent(),
            "local_generation_note":"Standard Wan 2.2 TI2V-5B documentation requires 24 GB VRAM with offload. Lower-memory community workflows need separate validation; a detected GPU alone does not mean inference is ready."}

@app.get("/api/providers")
def providers():
    return provider_catalog()

@app.get("/api/settings")
def settings():
    return read_settings()

@app.patch("/api/settings")
def save_settings(body: dict):
    if "youtube_refresh_token" in body:
        raise HTTPException(422,"OAuth refresh tokens are managed by the server.")
    try:
        return update_settings(body)
    except ValueError as exc:
        raise HTTPException(422,redact(exc)) from exc

@app.get("/api/projects")
def projects():
    with SessionLocal() as session:
        return [project_json(row) for row in session.scalars(select(Project).order_by(Project.updated_at.desc()))]

@app.post("/api/projects",status_code=201)
def create_project(body: ProjectInput):
    payload = body.model_dump()
    payload["output_asset_id"] = None
    with SessionLocal() as session:
        validate_refs(session,payload)
        row = Project(payload=payload)
        session.add(row); session.commit()
        return project_json(row)

@app.get("/api/projects/{project_id}")
def project(project_id: str):
    with SessionLocal() as session:
        return project_json(get_project(session,project_id))

@app.patch("/api/projects/{project_id}")
def edit_project(project_id: str, body: dict):
    if set(body) & {"id","created_at","updated_at","output_asset_id"}:
        raise HTTPException(422,"Project identifiers and output links are server managed.")
    with SessionLocal() as session:
        transaction_lock(session)
        row = get_project(session,project_id,True)
        idle_project(session,project_id)
        merged = {**upgrade_legacy_project(row.payload),**body}
        if "settings" in body:
            if not isinstance(body["settings"],dict):
                raise HTTPException(422,"Settings must be an object.")
            merged["settings"] = {**upgrade_legacy_project(row.payload)["settings"],**body["settings"]}
        try:
            payload = ProjectInput.model_validate(merged).model_dump()
        except ValidationError as exc:
            raise HTTPException(422,str(exc)) from exc
        old_scenes={scene["id"]:scene for scene in row.payload.get("scenes",[])}
        for scene in payload["scenes"]:
            old=old_scenes.get(scene["id"])
            if old and scene.get("asset_id")==old.get("asset_id") and any(scene.get(key)!=old.get(key) for key in ("prompt","duration")):
                scene["asset_id"]=None
                scene["status"]="draft"
        if any(payload.get(k)!=row.payload.get(k) for k in ("prompt","mode","provider")) or any(payload["settings"].get(k)!=row.payload["settings"].get(k) for k in ("image_asset_id","style","camera","quality","seed","character_ids")):
            # Changing the overall inference source invalidates cached scenes.
            for scene in payload["scenes"]:
                scene["asset_id"]=None
                scene["status"]="draft"
        validate_refs(session,payload)
        # Keep the last export for comparison; it is not asserted to reflect unsaved/new edits.
        row.payload=payload; row.updated_at=now();session.commit()
        return project_json(row)

@app.delete("/api/projects/{project_id}",status_code=204)
def remove_project(project_id: str):
    with SessionLocal() as session:
        transaction_lock(session)
        row=get_project(session,project_id,True);idle_project(session,project_id)
        job_ids=list(session.scalars(select(Job.id).where(Job.project_id==project_id)))
        if job_ids:
            session.execute(delete(Usage).where(Usage.job_id.in_(job_ids)))
        session.execute(delete(Job).where(Job.project_id==project_id))
        session.delete(row);session.commit()
    return Response(status_code=204)

@app.get("/api/characters")
def characters():
    with SessionLocal() as session:
        return [{**row.payload,"id":row.id,"created_at":row.created_at} for row in session.scalars(select(Character).order_by(Character.created_at.desc()))]

@app.post("/api/characters",status_code=201)
def create_character(body: CharacterInput):
    with SessionLocal() as session:
        if body.reference_asset_id:
            asset=session.get(Asset,body.reference_asset_id)
            if not asset or asset.kind!="image":
                raise HTTPException(422,"Character reference must be an uploaded image.")
        row=Character(payload=body.model_dump());session.add(row);session.commit()
        return {**row.payload,"id":row.id,"created_at":row.created_at}

@app.delete("/api/characters/{character_id}",status_code=204)
def remove_character(character_id: str):
    with SessionLocal() as session:
        row=session.get(Character,character_id)
        if not row:
            raise HTTPException(404,"Character not found.")
        for project in session.scalars(select(Project)):
            if character_id in project.payload["settings"].get("character_ids",[]):
                raise HTTPException(409,"Remove this character from its projects before deleting.")
        session.delete(row);session.commit()
    return Response(status_code=204)

@app.post("/api/assets",status_code=201)
async def upload_asset(file: UploadFile = File(...)):
    original=(file.filename or "upload").replace("\\","/").split("/")[-1][:240]
    suffix=Path(original).suffix.lower()
    kind = "image" if suffix in {".jpg",".jpeg",".png",".webp"} else "video" if suffix in {".mp4",".webm",".mov"} else "audio" if suffix in {".mp3",".wav",".m4a",".ogg"} else None
    if not kind:
        raise HTTPException(422,"Allowed files: PNG, JPEG, WebP, MP4, WebM, MOV, MP3, WAV, M4A, OGG.")
    if shutil.disk_usage(DATA_DIR).free < 1024**3:
        raise HTTPException(507,"Keep at least 1 GB free for uploads and rendering.")
    asset_id=uid();target=ASSET_DIR/(asset_id+suffix);written=0
    try:
        with target.open("xb") as output:
            while chunk:=await file.read(1024*1024):
                written+=len(chunk)
                if written > (20*1024*1024 if kind=="image" else MAX_UPLOAD):
                    raise HTTPException(413,"File exceeds upload limit (20 MB images; 100 MB video/audio).")
                output.write(chunk)
        if not written:
            raise HTTPException(422,"Empty files are not supported.")
        if kind=="image":
            with Image.open(target) as image:
                if image.width*image.height>40_000_000:
                    raise HTTPException(422,"Image exceeds 40 megapixels.")
                image.verify()
            with Image.open(target) as image:
                meta={"width":image.width,"height":image.height,"format":image.format,"size_bytes":written}
        else:
            meta=await run_in_threadpool(probe,target)
            if kind=="video" and (not meta["width"] or not 0<meta["duration"]<=300):
                raise HTTPException(422,"Video must contain a valid stream and last at most 5 minutes.")
            if kind=="audio" and (not meta["has_audio"] or meta["width"] or not 0<meta["duration"]<=600):
                raise HTTPException(422,"Audio must contain only audio and last at most 10 minutes.")
        with SessionLocal() as session:
            row=Asset(id=asset_id,path=str(target),name=original,kind=kind,meta=meta)
            session.add(row);session.commit()
            return asset_json(row)
    except HTTPException:
        target.unlink(missing_ok=True);raise
    except (ValueError,RuntimeError,OSError,UnidentifiedImageError,Image.DecompressionBombError) as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(422,"Invalid or unsupported media file.") from exc
    finally:
        await file.close()

@app.get("/api/assets")
def assets():
    with SessionLocal() as session:
        return [asset_json(row) for row in session.scalars(select(Asset).order_by(Asset.created_at.desc()))]

@app.get("/api/assets/{asset_id}/file")
def asset_file(asset_id: str, download: bool = False):
    with SessionLocal() as session:
        row=session.get(Asset,asset_id)
        if not row:
            raise HTTPException(404,"Asset not found.")
        path=storage_path(row.path)
        return FileResponse(path, filename=row.name, content_disposition_type="attachment" if download else "inline")

@app.get("/api/assets/{asset_id}/subtitles")
def subtitles(asset_id: str):
    with SessionLocal() as session:
        row=session.get(Asset,asset_id)
        if not row:
            raise HTTPException(404,"Asset not found.")
        target=Path(row.path).with_suffix(".srt")
        storage_path(str(target))
        return FileResponse(target,media_type="application/x-subrip",filename=target.name)

@app.get("/api/assets/{asset_id}/captions.vtt")
def browser_captions(asset_id: str):
    with SessionLocal() as session:
        row=session.get(Asset,asset_id)
        if not row:
            raise HTTPException(404,"Asset not found.")
        target=storage_path(str(Path(row.path).with_suffix(".srt")))
        content=target.read_text(encoding="utf-8-sig")
        content=re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})",r"\1.\2",content)
        return Response("WEBVTT\n\n"+content,media_type="text/vtt")


def enqueue(project_id: str, body: GenerateInput, render_only: bool):
    with SessionLocal() as session:
        transaction_lock(session)
        row=get_project(session,project_id,True);idle_project(session,project_id)
        if len(list(session.scalars(select(Job.id).where(Job.status.in_(ACTIVE)).limit(MAX_QUEUED))))>=MAX_QUEUED:
            raise HTTPException(429,"The local job queue is full.")
        today=datetime.now(timezone.utc).date().isoformat()
        count=len(list(session.scalars(select(Job.id).where(Job.created_at>=today))))
        if count>=DAILY_QUOTA:
            raise HTTPException(429,"Daily job quota reached. Adjust STORYORBIT_DAILY_QUOTA on the server if needed.")
        if shutil.disk_usage(DATA_DIR).free<2*1024**3:
            raise HTTPException(507,"Keep at least 2 GB free before starting a job.")
        payload=upgrade_legacy_project(row.payload)
        validate_refs(session,payload)
        if not payload.get("scenes") and not payload.get("prompt","").strip():
            raise HTTPException(422,"Enter a prompt or add at least one scene.")
        if body.scene_id and not any(s["id"]==body.scene_id for s in payload.get("scenes",[])):
            raise HTTPException(404,"Scene not found.")
        if render_only:
            if not payload["scenes"] or any(not s.get("asset_id") for s in payload["scenes"]):
                raise HTTPException(409,"Generate or import every scene clip before rendering.")
        else:
            try:
                validate_request(payload)
            except ProviderError as exc:
                raise HTTPException(409,str(exc)) from exc
        if not body.scene_id and payload.get("scenes"):
            try:
                if render_only:
                    actual = [{**s, "duration": session.get(Asset, s["asset_id"]).meta.get("duration", 0)} for s in payload["scenes"]]
                    require_timeline(actual, payload["settings"]["duration"])
                else:
                    require_timeline(payload["scenes"], payload["settings"]["duration"])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        if payload["settings"].get("narration_enabled") and not read_settings()["tts_configured"]:
            raise HTTPException(409,"Configure a voice provider or turn narration off.")
        try:
            binary("ffmpeg");binary("ffprobe")
        except RuntimeError as exc:
            raise HTTPException(409,str(exc)) from exc
        if body.scene_id:
            for scene in payload["scenes"]:
                if scene["id"]==body.scene_id:
                    scene["asset_id"]=None;scene["status"]="draft"
        job=Job(project_id=project_id,payload={"project":payload,"scene_id":body.scene_id,"render_only":render_only,"handles":{},"submitted":{}},
                logs=[{"time":now(),"message":"Job saved to the durable queue. Progress measures completed pipeline stages, not an inference time estimate."}])
        session.add(job);session.commit()
        return job_json(job)

@app.post("/api/projects/{project_id}/generate",status_code=202)
def generate(project_id: str, body: GenerateInput = Body(default=GenerateInput())):
    return enqueue(project_id,body,False)

@app.post("/api/projects/{project_id}/render",status_code=202)
def render(project_id: str):
    return enqueue(project_id,GenerateInput(),True)

@app.get("/api/jobs")
def jobs():
    with SessionLocal() as session:
        return [job_json(row) for row in session.scalars(select(Job).order_by(Job.created_at.desc()).limit(200))]

@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    with SessionLocal() as session:
        row=session.get(Job,job_id)
        if not row:
            raise HTTPException(404,"Job not found.")
        return job_json(row)

@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    with SessionLocal() as session:
        row=session.get(Job,job_id)
        if not row:
            raise HTTPException(404,"Job not found.")
        if row.status not in ACTIVE:
            raise HTTPException(409,"Only queued or running jobs can be cancelled.")
        row.cancel_requested=True;row.updated_at=now()
        if row.status=="queued":
            row.status="cancelled";row.stage="cancelled"
        row.logs=[*row.logs,{"time":now(),"message":"Cancellation requested. An in-progress media operation may finish; remote compute may still incur charges."}][-200:]
        session.commit();return job_json(row)

@app.post("/api/jobs/{job_id}/retry",status_code=202)
def retry(job_id: str,body: RetryInput = Body(default=RetryInput())):
    with SessionLocal() as session:
        transaction_lock(session)
        row=session.get(Job,job_id)
        if not row:
            raise HTTPException(404,"Job not found.")
        if row.status not in {"failed","cancelled","needs_review"}:
            raise HTTPException(409,"This job cannot be retried.")
        idle_project(session,row.project_id)
        if row.status=="needs_review" and not body.acknowledge_duplicate_risk:
            raise HTTPException(409,"Check the provider dashboard first. Retrying an uncertain submission may duplicate a charge; send acknowledge_duplicate_risk=true to proceed.")
        payload=copy.deepcopy(row.payload)
        current_project=session.get(Project,row.project_id)
        if payload.get("project") and current_project.payload != payload["project"]:
            raise HTTPException(409,"This project was edited after the failed job. Start a new generation or render to preserve your edits.")
        if row.status=="needs_review":
            for scene_id,started in list(payload.get("submitted",{}).items()):
                if scene_id not in payload.get("handles",{}):
                    payload["submitted"].pop(scene_id,None)
        # Only a confirmed terminal remote request is discarded. Lost/unknown
        # submissions retain receipts and must never be silently re-submitted.
        for scene_id in payload.pop("terminal_handles", []):
            payload.get("handles", {}).pop(scene_id, None)
            payload.get("submitted", {}).pop(scene_id, None)
        payload.pop("failure",None)
        row.payload=payload;row.status="queued";row.stage="queued";row.error=None;row.cancel_requested=False
        row.worker_id=None;row.lease_until=None;row.updated_at=now()
        row.logs=[*row.logs,{"time":now(),"message":"Explicit retry queued; completed scene assets will be reused."}][-200:]
        session.commit();return job_json(row)

@app.get("/api/usage")
def usage():
    with SessionLocal() as session:
        rows=list(session.scalars(select(Usage).order_by(Usage.created_at.desc()).limit(500)))
        return {"requests":[{"id":r.id,"job_id":r.job_id,"provider":r.provider,"request_id":r.request_id,"duration_seconds":r.duration_seconds,"cost_usd":r.cost_usd,"created_at":r.created_at} for r in rows],
                "cost_note":"Request usage is recorded. Dollar cost remains unknown unless reported by a provider; check its billing dashboard."}

from .creative import router as creative_router
app.include_router(creative_router)
try:
    from .youtube import router as youtube_router
    app.include_router(youtube_router)
except ModuleNotFoundError as exc:
    if exc.name not in {"app.youtube",__package__+".youtube"}:
        raise

