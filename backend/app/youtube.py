"""Opt-in YouTube OAuth and an explicit-approval resumable publishing queue."""
from __future__ import annotations
import base64, hashlib, hmac, json, secrets, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from typing import Literal
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import String, Text, JSON, Integer, select, update
from sqlalchemy.orm import Mapped, mapped_column
from .config import DATA_DIR, read_settings, update_settings, _cipher, redact
from .db import Base, SessionLocal
from .models import Asset, Project, uid, now
from .media import validate_video

router = APIRouter(prefix="/api/youtube", tags=["youtube"])
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"

class PublishDraft(Base):
    __tablename__ = "youtube_drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(32))
    asset_id: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    upload_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)

class DraftInput(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    privacy: Literal["private", "unlisted", "public"] = "private"
    publish_at: datetime | None = None
    made_for_kids: bool = False
    @model_validator(mode="after")
    def validate_schedule(self):
        if any(len(tag)>100 for tag in self.tags) or sum(map(len,self.tags)) > 400:
            raise ValueError("Tags are too long.")
        if self.publish_at:
            if self.publish_at.tzinfo is None:
                raise ValueError("Scheduled time must include a timezone.")
            if self.publish_at <= datetime.now(timezone.utc):
                raise ValueError("Scheduled release must be in the future.")
            if self.privacy != "public":
                raise ValueError("Scheduled release requires public visibility; YouTube keeps it private until then.")
        return self

def public(draft: PublishDraft) -> dict:
    return {"id":draft.id,"project_id":draft.project_id,**draft.payload,"status":draft.status,
            "uploaded_bytes":draft.uploaded_bytes,"total_bytes":draft.total_bytes,
            "error":draft.error,"video_id":draft.video_id,"created_at":draft.created_at}

def require_enabled() -> dict:
    config = read_settings(True)
    if not config.get("youtube_enabled"):
        raise HTTPException(409, "Enable YouTube publishing in Settings first.")
    if not config.get("youtube_client_id") or not config.get("youtube_client_secret"):
        raise HTTPException(409, "Configure your Google OAuth client before connecting YouTube.")
    return config

@router.get("/status")
def status():
    config = read_settings(True)
    return {"enabled":bool(config.get("youtube_enabled")),
            "configured":bool(config.get("youtube_client_id") and config.get("youtube_client_secret")),
            "authenticated":bool(config.get("youtube_refresh_token")), "approval_required":True,
            "synthetic_media_disclosure":True}

@router.post("/connect")
def connect(response: Response):
    config = require_enabled()
    redirect = config.get("youtube_redirect_uri","http://127.0.0.1:8000/api/youtube/callback")
    parsed = urlparse(redirect)
    if parsed.scheme not in {"http","https"} or not parsed.hostname or (parsed.scheme=="http" and parsed.hostname not in {"127.0.0.1","localhost"}):
        raise HTTPException(422, "OAuth redirect must use HTTPS or localhost.")
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    record = {"hash":hashlib.sha256(nonce.encode()).hexdigest(),"verifier":verifier,"expires":time.time()+600}
    (DATA_DIR/"youtube-oauth.enc").write_bytes(_cipher().encrypt(json.dumps(record).encode()))
    response.set_cookie("storyorbit_oauth",nonce,max_age=600,httponly=True,samesite="lax",secure=parsed.scheme=="https",path="/")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = {"client_id":config["youtube_client_id"],"redirect_uri":redirect,"response_type":"code",
             "scope":SCOPE,"access_type":"offline","prompt":"consent","state":nonce,
             "code_challenge":challenge,"code_challenge_method":"S256"}
    return {"url":AUTH+"?"+urlencode(query)}

@router.get("/callback", response_class=HTMLResponse)
def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    config = require_enabled()
    cookie = request.cookies.get("storyorbit_oauth","")
    record_path = DATA_DIR/"youtube-oauth.enc"
    if not state or not cookie or not hmac.compare_digest(state,cookie) or not record_path.exists():
        raise HTTPException(400, "OAuth state invalid or expired. Connect again from Settings.")
    record = json.loads(_cipher().decrypt(record_path.read_bytes()))
    record_path.unlink(missing_ok=True)
    if time.time()>record["expires"] or not hmac.compare_digest(record["hash"],hashlib.sha256(state.encode()).hexdigest()):
        raise HTTPException(400, "OAuth state expired.")
    if error or not code:
        raise HTTPException(400, "YouTube authorization was not completed.")
    with httpx.Client(timeout=30,trust_env=False) as client:
        result = client.post(TOKEN,data={"client_id":config["youtube_client_id"],
            "client_secret":config["youtube_client_secret"],"code":code,"grant_type":"authorization_code",
            "redirect_uri":config["youtube_redirect_uri"],"code_verifier":record["verifier"]})
    if result.status_code != 200 or not result.json().get("refresh_token"):
        raise HTTPException(502, "Google did not issue offline authorization. Reconnect and grant access.")
    update_settings({"youtube_refresh_token":result.json()["refresh_token"]})
    response = HTMLResponse("<!doctype html><html><head><title>YouTube connected</title></head><body style='background:#111019;color:#efedf8;font:18px system-ui;padding:4rem'><h1>YouTube connected</h1><p>Return to StoryOrbit Settings. Publishing still requires approval for each video.</p></body></html>")
    response.delete_cookie("storyorbit_oauth",path="/")
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

@router.post("/disconnect")
def disconnect():
    config=read_settings(True)
    token=config.get("youtube_refresh_token")
    if token:
        try:
            result=httpx.post("https://oauth2.googleapis.com/revoke",data={"token":token},timeout=20)
            if result.status_code not in (200,400):
                raise HTTPException(502,"Google could not revoke access. Retry disconnect.")
        except httpx.RequestError as exc:
            raise HTTPException(502,"Google could not be reached to revoke access.") from exc
    update_settings({"youtube_refresh_token":"","youtube_enabled":False})
    return {"disconnected":True}

@router.get("/drafts")
def list_drafts():
    with SessionLocal() as db:
        return [public(x) for x in db.scalars(select(PublishDraft).order_by(PublishDraft.created_at.desc()).limit(200))]

@router.post("/drafts")
def draft(body: DraftInput):
    with SessionLocal() as db:
        project=db.get(Project,body.project_id)
        asset=db.get(Asset,project.payload.get("output_asset_id")) if project and project.payload.get("output_asset_id") else None
        if not asset or asset.kind!="video":
            raise HTTPException(409,"Render a completed project before preparing a YouTube upload.")
        payload=body.model_dump(mode="json",exclude={"project_id"})
        record=PublishDraft(project_id=body.project_id,asset_id=asset.id,payload=payload,total_bytes=Path(asset.path).stat().st_size)
        db.add(record); db.commit(); db.refresh(record)
        return public(record)

@router.post("/drafts/{draft_id}/approve")
def approve(draft_id:str):
    config=require_enabled()
    if not config.get("youtube_refresh_token"):
        raise HTTPException(409,"Connect your YouTube account first.")
    with SessionLocal() as db:
        record=db.get(PublishDraft,draft_id)
        if not record:
            raise HTTPException(404,"Draft not found.")
        if record.status not in {"draft","failed","needs_review"}:
            raise HTTPException(409,"This upload cannot be approved in its current state.")
        if record.status=="needs_review" and not record.upload_session:
            raise HTTPException(409,"Previous upload submission is uncertain. Check YouTube Studio before creating a new draft.")
        if record.payload.get("publish_at") and datetime.fromisoformat(record.payload["publish_at"])<=datetime.now(timezone.utc):
            raise HTTPException(409,"Scheduled time has passed. Create a draft with a future release time.")
        record.status="approved"; record.error=None; record.updated_at=now(); db.commit()
        return public(record)

@router.post("/drafts/{draft_id}/cancel")
def cancel(draft_id:str):
    with SessionLocal() as db:
        record=db.get(PublishDraft,draft_id)
        if not record:
            raise HTTPException(404,"Draft not found.")
        if record.status not in {"draft","approved","failed","needs_review"}:
            raise HTTPException(409,"An upload already in progress cannot be cancelled here.")
        record.status="cancelled"; record.updated_at=now(); db.commit()
        return public(record)

def _upload_url(value: str) -> str:
    parsed=urlparse(value)
    if parsed.scheme!="https" or parsed.hostname!="www.googleapis.com" or not parsed.path.startswith("/upload/youtube/") or parsed.username or parsed.password:
        raise RuntimeError("Google returned an invalid resumable upload URL.")
    return value

def _token(config:dict) -> str:
    result=httpx.post(TOKEN,data={"client_id":config["youtube_client_id"],
        "client_secret":config["youtube_client_secret"],"refresh_token":config["youtube_refresh_token"],
        "grant_type":"refresh_token"},timeout=30)
    if result.status_code!=200:
        raise RuntimeError("YouTube authorization expired or was revoked. Reconnect your account.")
    return result.json()["access_token"]

def _save(draft_id:str,**values):
    with SessionLocal() as db:
        values["updated_at"]=now()
        db.execute(update(PublishDraft).where(PublishDraft.id==draft_id).values(**values));db.commit()

def _finish(draft_id:str,response:httpx.Response):
    data=response.json()
    if not data.get("id"):
        raise RuntimeError("YouTube did not return a video ID.")
    _save(draft_id,status="uploaded",video_id=data["id"],error=None,lease_until=None)

def process_next_publish() -> bool:
    """Called by the separate worker. Never uploads without persisted explicit approval."""
    config=read_settings(True)
    if not config.get("youtube_enabled") or not config.get("youtube_refresh_token"):
        return False
    with SessionLocal() as db:
        # Resume only a known server session after a crash; uncertain submissions need review.
        stale=list(db.scalars(select(PublishDraft).where(PublishDraft.status=="uploading",PublishDraft.lease_until<time.time())))
        for record in stale:
            record.status="approved" if record.upload_session else "needs_review"
            record.error="Worker stopped during upload; known sessions can resume." if record.upload_session else "Upload submission uncertain. Check YouTube Studio."
        db.commit()
        record=db.scalar(select(PublishDraft).where(PublishDraft.status=="approved").order_by(PublishDraft.created_at).limit(1))
        if not record:
            return False
        draft_id=record.id
        claimed=db.execute(update(PublishDraft).where(PublishDraft.id==draft_id,PublishDraft.status=="approved").values(status="uploading",lease_until=time.time()+300))
        db.commit()
        if claimed.rowcount!=1:
            return False
        db.refresh(record)
        asset=db.get(Asset,record.asset_id)
        path=Path(asset.path) if asset else None
        payload=dict(record.payload)
        session_url=_cipher().decrypt(record.upload_session.encode()).decode() if record.upload_session else None
    submission_started = False
    try:
        if not path or not path.is_file():
            raise RuntimeError("The approved video file is missing.")
        validate_video(path)
        token=_token(config)
        headers={"Authorization":"Bearer "+token}
        size=path.stat().st_size
        with httpx.Client(timeout=120,follow_redirects=False,trust_env=False) as client:
            if not session_url:
                video_status={"privacyStatus":"private" if payload.get("publish_at") else payload["privacy"],
                              "selfDeclaredMadeForKids":payload["made_for_kids"],"containsSyntheticMedia":True}
                if payload.get("publish_at"):
                    video_status["publishAt"]=payload["publish_at"]
                submission_started = True
                result=client.post(UPLOAD,params={"uploadType":"resumable","part":"snippet,status","notifySubscribers":"false"},
                    headers={**headers,"X-Upload-Content-Type":"video/mp4","X-Upload-Content-Length":str(size)},
                    json={"snippet":{"title":payload["title"],"description":payload["description"],"tags":payload["tags"],"categoryId":"24"},
                          "status":video_status})
                if result.status_code>=500:
                    raise RuntimeError("YouTube upload submission is uncertain. Check YouTube Studio.")
                if 400 <= result.status_code < 500:
                    submission_started = False
                result.raise_for_status()
                session_url=_upload_url(result.headers["location"])
                _save(draft_id,upload_session=_cipher().encrypt(session_url.encode()).decode(),total_bytes=size)
                offset=0
            else:
                result=client.put(_upload_url(session_url),headers={**headers,"Content-Length":"0","Content-Range":f"bytes */{size}"},content=b"")
                if result.status_code in (200,201):
                    _finish(draft_id,result);return True
                if result.status_code!=308:
                    raise RuntimeError("The resumable YouTube session is unavailable. Inspect YouTube Studio before creating another draft.")
                offset=int(result.headers["Range"].split("-")[-1])+1 if "Range" in result.headers else 0
            with path.open("rb") as source:
                source.seek(offset)
                while offset<size:
                    # Stop between chunks if the owner disables publishing.
                    if not read_settings(True).get("youtube_enabled"):
                        _save(draft_id,status="failed",error="Publishing was disabled. The remote session is retained.",lease_until=None)
                        return True
                    chunk=source.read(8*1024*1024)
                    end=offset+len(chunk)-1
                    _save(draft_id,lease_until=time.time()+300)
                    result=client.put(_upload_url(session_url),headers={**headers,"Content-Type":"video/mp4",
                        "Content-Length":str(len(chunk)),"Content-Range":f"bytes {offset}-{end}/{size}"},content=chunk)
                    if result.status_code in (200,201):
                        _save(draft_id,uploaded_bytes=size);_finish(draft_id,result);return True
                    if result.status_code!=308:
                        result.raise_for_status()
                        raise RuntimeError("Unexpected YouTube upload response.")
                    offset=int(result.headers["Range"].split("-")[-1])+1 if "Range" in result.headers else 0
                    if offset<end+1:
                        raise RuntimeError("YouTube acknowledged fewer bytes; retry to query and resume the session.")
                    _save(draft_id,uploaded_bytes=offset)
        raise RuntimeError("Upload ended without a confirmed YouTube video ID.")
    except Exception as exc:
        _save(draft_id,status="needs_review" if submission_started and not session_url else "failed",error=(f"YouTube HTTP {exc.response.status_code}; retry or reconnect." if isinstance(exc,httpx.HTTPStatusError) else "YouTube network request failed; the session is retained when known." if isinstance(exc,httpx.RequestError) else redact(exc)),lease_until=None)
        return True

