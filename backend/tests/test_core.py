"""Core integration and mocked provider contracts. No live inference is exercised."""
import copy, io, json, shutil, time
from pathlib import Path
import httpx, pytest
from PIL import Image
from fastapi.testclient import TestClient
from app import main, providers, worker, media
from app.config import DATA_DIR, read_settings, update_settings
from app.db import SessionLocal, engine
from app.models import Project, Asset, Job
from app.schemas import ProjectInput, Settings

def project(client, **overrides):
    payload={"title":"API test","prompt":"An original cinematic scene","settings":{"resolution":"480p","subtitles":False},**overrides}
    response=client.post("/api/projects",json=payload)
    assert response.status_code==201,response.text
    return response.json()

def raw_job(project_id, payload=None, **kwargs):
    with SessionLocal() as db:
        value=Job(project_id=project_id,payload=payload or {},**kwargs)
        db.add(value);db.commit();return value.id

def test_project_persists_after_database_reconnect(client):
    row=project(client,title="चाय की कहानी")
    edited=client.patch("/api/projects/"+row["id"],json={"title":"Saved Hindi story"})
    assert edited.status_code==200
    engine.dispose()
    with TestClient(main.app) as reopened:
        assert reopened.get("/api/projects/"+row["id"]).json()["title"]=="Saved Hindi story"
    assert client.delete("/api/projects/"+row["id"]).status_code==204
    assert client.get("/api/projects/"+row["id"]).status_code==404

def test_schema_rejects_unsupported_settings_and_duplicate_scene_ids(client):
    assert client.post("/api/projects",json={"settings":{"duration":15}}).status_code==422
    assert client.post("/api/projects",json={"scenes":[{"id":"same","prompt":"A"},{"id":"same","prompt":"B"}]}).status_code==422

def test_server_manages_completed_output(client):
    row=project(client,output_asset_id="forged")
    assert row["output_asset_id"] is None
    assert client.patch("/api/projects/"+row["id"],json={"output_asset_id":"forged"}).status_code==422

def test_credentials_encrypted_and_never_returned(client):
    secret="TEST_ONLY_FAKE_SECRET_123"
    saved=client.patch("/api/settings",json={"fal_key":secret})
    assert saved.status_code==200
    assert saved.json()["fal_configured"]
    assert secret not in saved.text and "fal_key" not in saved.json()
    assert secret not in (DATA_DIR/"settings.json").read_text()
    assert read_settings(True)["fal_key"]==secret
    assert client.patch("/api/settings",json={"youtube_refresh_token":secret}).status_code==422
    assert client.patch("/api/settings",json={"youtube_auto_approve":True}).status_code==422

def test_auth_and_origin_protection(client,monkeypatch):
    assert client.post("/api/projects",json={},headers={"Origin":"https://evil.example"}).status_code==403
    monkeypatch.setenv("STORYORBIT_API_TOKEN","TEST_TOKEN")
    assert client.get("/api/projects").status_code==401
    assert client.get("/api/projects",headers={"Authorization":"Bearer TEST_TOKEN"}).status_code==200

def test_chunked_oversize_request_is_bounded(client):
    response=client.post("/api/projects",content=iter([b"x"*(1024*1024)]*3),headers={"Content-Type":"application/json"})
    assert response.status_code==413

def test_malformed_content_length_rejected(client):
    response=client.post("/api/projects",content=b"{}",headers={"Content-Type":"application/json","Content-Length":"bad"})
    assert response.status_code==400

def test_invalid_upload_rejected_and_image_name_sanitized(client):
    assert client.post("/api/assets",files={"file":("bad.mp4",b"not video","video/mp4")}).status_code==422
    assert client.post("/api/assets",files={"file":("bad.exe",b"oops","application/octet-stream")}).status_code==422
    content=io.BytesIO();Image.new("RGB",(64,64),"blue").save(content,format="PNG")
    uploaded=client.post("/api/assets",files={"file":("../../outside.png",content.getvalue(),"image/png")})
    assert uploaded.status_code==201,uploaded.text
    assert uploaded.json()["name"]=="outside.png"
    with SessionLocal() as db:
        asset=db.get(Asset,uploaded.json()["id"])
        assert Path(asset.path).is_relative_to(DATA_DIR)
    assert client.get(uploaded.json()["url"]).status_code==200

def test_generate_without_provider_is_not_queued(client):
    row=project(client)
    response=client.post(f"/api/projects/{row['id']}/generate",json={})
    assert response.status_code==409
    assert client.get("/api/jobs").json()==[]

def test_cancel_queued_job_does_not_execute(client):
    row=project(client)
    job_id=raw_job(row["id"])
    assert client.post(f"/api/jobs/{job_id}/cancel",json={}).json()["status"]=="cancelled"
    assert worker.claim_job("worker-a") is None

def test_claim_is_global_across_workers(client):
    one=project(client);two=project(client)
    first=raw_job(one["id"]);raw_job(two["id"])
    assert worker.claim_job("worker-a")==first
    assert worker.claim_job("worker-b") is None

def test_recovery_retains_receipts_and_marks_ambiguous_submits(client):
    row=project(client)
    known=raw_job(row["id"],{"submitted":{"s":"then"},"handles":{"s":{"request_id":"known"}}},status="running",lease_until=time.time()-10)
    unknown=raw_job(row["id"],{"submitted":{"s":"then"},"handles":{}},status="running",lease_until=time.time()-10)
    worker.recover_jobs()
    with SessionLocal() as db:
        assert db.get(Job,known).status=="queued"
        assert db.get(Job,known).payload["handles"]["s"]["request_id"]=="known"
        assert db.get(Job,unknown).status=="needs_review"

def test_uncertain_retry_requires_explicit_acknowledgment(client):
    row=project(client)
    job_id=raw_job(row["id"],{"submitted":{"s":"then"},"handles":{}},status="needs_review")
    assert client.post(f"/api/jobs/{job_id}/retry",json={}).status_code==409
    result=client.post(f"/api/jobs/{job_id}/retry",json={"acknowledge_duplicate_risk":True})
    assert result.status_code==202
    with SessionLocal() as db:
        assert db.get(Job,job_id).payload["submitted"]=={}

def test_confirmed_terminal_retry_drops_only_terminal_receipt(client):
    row=project(client)
    job_id=raw_job(row["id"],{"submitted":{"a":"then","b":"then"},"handles":{"a":{"request_id":"failed"},"b":{"request_id":"keep"}},"terminal_handles":["a"]},status="failed")
    assert client.post(f"/api/jobs/{job_id}/retry",json={}).status_code==202
    with SessionLocal() as db:
        payload=db.get(Job,job_id).payload
        assert "a" not in payload["handles"] and payload["handles"]["b"]["request_id"]=="keep"

def test_fal_submission_contract_preserves_receipt_and_portrait_settings(monkeypatch):
    sent=[]
    def post(url,**kwargs):
        sent.append(kwargs)
        return httpx.Response(200,json={"request_id":"fixture",
            "status_url":"https://queue.fal.run/fal-ai/wan/requests/fixture/status",
            "response_url":"https://queue.fal.run/fal-ai/wan/requests/fixture",
            "cancel_url":"https://queue.fal.run/fal-ai/wan/requests/fixture/cancel"})
    monkeypatch.setattr(providers.httpx,"post",post)
    handle=providers.FalProvider({"fal_key":"TEST"}).submit("A scene",{**Settings().model_dump(), "duration":5})
    assert handle["request_id"]=="fixture"
    assert sent[0]["json"]["num_frames"]==81
    assert sent[0]["json"]["aspect_ratio"]=="9:16"
    assert sent[0]["json"]["interpolator_model"]=="none"

@pytest.mark.parametrize("body",[[],None,{},{"request_id":"r","status_url":"https://evil.example"}])
def test_successful_but_invalid_receipt_is_uncertain(monkeypatch,body):
    monkeypatch.setattr(providers.httpx,"post",lambda *a,**k:httpx.Response(200,json=body))
    with pytest.raises(providers.SubmissionUnknown):
        providers.FalProvider({"fal_key":"TEST"}).submit("A scene",{**Settings().model_dump(), "duration":5})

def test_poll_timeout_retains_known_request(client,monkeypatch):
    row=project(client)
    job_id=raw_job(row["id"],{"handles":{"s":{"request_id":"known"}}})
    monkeypatch.setattr(worker,"MAX_POLL_SECONDS",-1)
    class NoPoll:
        def poll(self,*_):pytest.fail("Timeout should be checked before another poll")
    with pytest.raises(RuntimeError,match="retained"):
        worker._wait_inference(job_id,NoPoll(),{"request_id":"known"},10)

def test_real_render_only_worker_and_browser_captions(client,tmp_path):
    # This is a technical FFmpeg fixture, NOT generated AI footage.
    fixture=tmp_path/"technical-fixture.mp4"
    media.run([media.binary("ffmpeg"),"-v","error","-y","-f","lavfi","-i",
        "color=c=blue:size=160x284:rate=16","-t","45","-c:v","libx264","-pix_fmt","yuv420p",str(fixture)])
    with fixture.open("rb") as source:
        result=client.post("/api/assets",files={"file":("technical-fixture.mp4",source,"video/mp4")})
    assert result.status_code==201,result.text
    asset_id=result.json()["id"]
    row=project(client,scenes=[{"id":"a","prompt":"Technical fixture only","narration":"नमस्ते","asset_id":asset_id,"duration":5}],
                settings={"resolution":"480p","subtitles":True})
    queued=client.post(f"/api/projects/{row['id']}/render",json={})
    assert queued.status_code==202,queued.text
    job_id=worker.claim_job("render-test")
    worker.process_job(job_id,"render-test")
    result=client.get(f"/api/jobs/{job_id}").json()
    assert result["status"]=="completed",result
    saved=client.get(f"/api/projects/{row['id']}").json()
    output=saved["output_asset_id"]
    assert output
    assert client.get(f"/api/assets/{output}/file").status_code==200
    captions=client.get(f"/api/assets/{output}/captions.vtt")
    assert captions.status_code==200 and captions.text.startswith("WEBVTT")
    assert "नमस्ते" in captions.text and "00:00:00.000" in captions.text
    with SessionLocal() as db:
        metadata=db.get(Asset,output).meta
        assert metadata["duration"] >= 45
        assert metadata["source"]=="assembled-scenes"
        assert metadata["scene_sources"][0]["source"]=="user-upload"


def test_local_fps_can_be_declared_but_fal_rejects_it():
    assert Settings(fps=24).fps==24
    update_settings({"fal_key":"TEST_ONLY"})
    payload=ProjectInput(prompt="A scene",settings=Settings(fps=24)).model_dump()
    with pytest.raises(providers.ProviderError,match="fps"):
        providers.validate_request(payload)

def test_cancelled_lease_recovery_does_not_leave_stuck_queue(client):
    row=project(client)
    job_id=raw_job(row["id"],{"submitted":{},"handles":{}},status="running",lease_until=time.time()-10,cancel_requested=True)
    worker.recover_jobs()
    with SessionLocal() as db:
        assert db.get(Job,job_id).status=="cancelled"

def test_retry_refuses_to_overwrite_newer_project_edits(client):
    row=project(client)
    with SessionLocal() as db:
        payload=copy.deepcopy(db.get(Project,row["id"]).payload)
    job_id=raw_job(row["id"],{"project":payload,"handles":{}},status="failed")
    assert client.patch("/api/projects/"+row["id"],json={"title":"Newer edits"}).status_code==200
    assert client.post(f"/api/jobs/{job_id}/retry",json={}).status_code==409
