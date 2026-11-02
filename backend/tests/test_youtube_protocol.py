"""Protocol-only YouTube tests. No network request or upload is made."""
from pathlib import Path
from datetime import datetime, timezone
import httpx
import pytest
from app import youtube
from app.db import init_db, SessionLocal
from app.models import Asset

def test_upload_requires_approved_draft_and_sends_disclosures(monkeypatch,tmp_path):
    init_db()
    clip=tmp_path/"mock-upload.bin"
    clip.write_bytes(b"local protocol fixture, not a video")
    with SessionLocal() as db:
        asset=Asset(path=str(clip),name="protocol-fixture.mp4",kind="video",meta={"test":True})
        db.add(asset);db.flush()
        draft=youtube.PublishDraft(project_id="protocol-test",asset_id=asset.id,status="approved",
            payload={"title":"Protocol test","description":"Fixture only","tags":[],"privacy":"private","made_for_kids":False,"publish_at":None})
        db.add(draft);db.commit();draft_id=draft.id
    sent=[]
    def transport(request):
        sent.append(request)
        if request.method=="POST":
            import json
            payload=json.loads(request.content)
            assert payload["status"]["privacyStatus"]=="private"
            assert payload["status"]["containsSyntheticMedia"] is True
            return httpx.Response(200,headers={"Location":"https://www.googleapis.com/upload/youtube/v3/videos?upload_id=fixture"})
        assert request.headers["Content-Range"].startswith("bytes 0-")
        return httpx.Response(201,json={"id":"TEST_ONLY_ID"})
    original_client=httpx.Client
    monkeypatch.setattr(youtube.httpx,"Client",lambda **kwargs: original_client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(youtube,"read_settings",lambda *_:{"youtube_enabled":True,"youtube_refresh_token":"TEST_ONLY"})
    monkeypatch.setattr(youtube,"_token",lambda _:"TEST_ONLY")
    monkeypatch.setattr(youtube,"validate_video",lambda _:{})
    assert youtube.process_next_publish()
    with SessionLocal() as db:
        record=db.get(youtube.PublishDraft,draft_id)
        assert record.status=="uploaded"
        assert record.video_id=="TEST_ONLY_ID"
        assert record.uploaded_bytes==clip.stat().st_size
        assert "upload_session" not in youtube.public(record)
        db.delete(record);db.delete(db.get(Asset,record.asset_id));db.commit()
    assert len(sent)==2

def test_unapproved_draft_does_not_call_network(monkeypatch):
    init_db()
    with SessionLocal() as db:
        draft=youtube.PublishDraft(project_id="test",asset_id="no-asset",status="draft",
            payload={"title":"Not approved"})
        db.add(draft);db.commit();draft_id=draft.id
    monkeypatch.setattr(youtube,"read_settings",lambda *_:{"youtube_enabled":True,"youtube_refresh_token":"TEST_ONLY"})
    monkeypatch.setattr(youtube,"_token",lambda _:pytest.fail("Unapproved draft must not request an access token"))
    assert not youtube.process_next_publish()
    with SessionLocal() as db:
        db.delete(db.get(youtube.PublishDraft,draft_id));db.commit()

