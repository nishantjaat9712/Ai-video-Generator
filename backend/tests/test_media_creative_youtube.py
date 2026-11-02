"""Local media tests and mocked service contracts. These do NOT verify AI inference."""
from pathlib import Path
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from pydantic import ValidationError
from app import media, creative, youtube

@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    folder=tmp_path_factory.mktemp("media-fixtures")
    paths=[]
    for index,color in enumerate(("blue","green")):
        path=folder/f"fixture-{index}.mp4"
        media.run([media.binary("ffmpeg"),"-v","error","-y","-f","lavfi","-i",
            f"color=c={color}:size=160x284:rate=16","-f","lavfi","-i",
            "sine=frequency=440:sample_rate=48000","-t","1.5","-c:v","libx264",
            "-pix_fmt","yuv420p","-c:a","aac",str(path)])
        paths.append(path)
    return paths

def test_assembly_reorders_trims_audio_and_unicode_captions(clips,tmp_path):
    output=tmp_path/"technical-test-only.mp4"
    result=media.assemble(list(reversed(clips)),output,
        [{"trim_start":0.25,"trim_end":1.25,"narration":"नमस्ते दुनिया"},
         {"trim_end":1,"narration":"A second scene"}],
        {"resolution":"480p","aspect_ratio":"9:16","fps":16,"subtitles":True,
         "language":"Hindi","music_path":str(clips[0]),"music_volume":0.1,"audio_volume":0.8})
    assert result["width"]==480 and result["height"]==854
    assert 1.9<=result["duration"]<=2.2 and result["has_audio"]
    assert any(s["codec_type"]=="subtitle" for s in result["streams"])
    captions=output.with_suffix(".srt").read_text(encoding="utf-8")
    assert "नमस्ते दुनिया" in captions and "00:00:01,000" in captions
    assert not list(tmp_path.glob("render-*"))

def test_trim_and_long_narration_are_rejected(clips,tmp_path):
    with pytest.raises(ValueError,match="trim"):
        media.assemble([clips[0]],tmp_path/"bad.mp4",[{"trim_start":2}],
            {"resolution":"480p","aspect_ratio":"9:16","fps":16})
    with pytest.raises(ValueError,match="narration"):
        media.assemble([clips[0]],tmp_path/"bad2.mp4",[{"trim_end":.5}],
            {"resolution":"480p","aspect_ratio":"9:16","fps":16},[clips[1]])

def test_invalid_video_is_not_success(tmp_path):
    path=tmp_path/"invalid.mp4";path.write_bytes(b"not video")
    with pytest.raises((RuntimeError,ValueError)):
        media.validate_video(path)

def test_reference_measurements_are_explicitly_nonsemantic(clips):
    result=media.analyze_reference(clips[0])
    assert result["width"]==160 and result["has_audio"]
    assert result["analysis_type"]=="Measured technical properties"
    assert "do not identify camera movement" in " ".join(result["limitations"])

def test_hindi_hook_is_not_scored_as_english():
    result=creative.hook_result("एक मजेदार कहानी","Hindi")
    assert result["supported"] is False and result["score"] is None
    english=creative.hook_result("Why do you waste 5 hours before you make one video?","English")
    assert english["supported"] and 0<=english["score"]<=100

def test_missing_llm_fails_without_fabricated_plan(monkeypatch):
    monkeypatch.setattr(creative,"read_settings",lambda *_:{"llm_configured":False})
    with pytest.raises(HTTPException) as error:
        creative.create_plan(creative.PlanRequest(text="A funny village story"))
    assert error.value.status_code==409

def test_llm_plan_contract_is_validated(monkeypatch):
    monkeypatch.setattr(creative,"_llm",lambda *_:{"title":"Demo","hook":"A hook","scenes":[{"prompt":"A detailed invented scene","duration":42}]})
    with pytest.raises(HTTPException) as error:
        creative.create_plan(creative.PlanRequest(text="A fictional story"))
    assert error.value.status_code==502

def test_no_narration_without_provider(monkeypatch,tmp_path):
    monkeypatch.setattr(creative,"read_settings",lambda *_:{"tts_configured":False})
    with pytest.raises(RuntimeError,match="Configure"):
        creative.synthesize("Hello",tmp_path/"voice.mp3","English")

def test_youtube_disabled_by_default(monkeypatch):
    monkeypatch.setattr(youtube,"read_settings",lambda *_:{})
    assert youtube.status()=={"enabled":False,"configured":False,"authenticated":False,"approval_required":True,"synthetic_media_disclosure":True}
    with pytest.raises(HTTPException) as error:
        youtube.connect(Response())
    assert error.value.status_code==409
    assert youtube.process_next_publish() is False

def test_youtube_oauth_rejects_missing_state_before_network(monkeypatch):
    monkeypatch.setattr(youtube,"read_settings",lambda *_:{"youtube_enabled":True,"youtube_client_id":"test","youtube_client_secret":"test"})
    request=Request({"type":"http","headers":[]})
    with pytest.raises(HTTPException) as error:
        youtube.callback(request,state="unexpected",code="not-real")
    assert error.value.status_code==400

@pytest.mark.parametrize("value",["http://www.googleapis.com/upload/youtube/v3/videos","https://evil.example/upload/youtube/v3/videos","https://user:pass@www.googleapis.com/upload/youtube/v3/videos"])
def test_youtube_upload_session_host_is_restricted(value):
    with pytest.raises(RuntimeError):
        youtube._upload_url(value)

def test_publish_schedule_requires_timezone_and_public_visibility():
    with pytest.raises(ValidationError):
        youtube.DraftInput(project_id="p",title="Test",publish_at="2099-01-01T10:00:00")
    with pytest.raises(ValidationError):
        youtube.DraftInput(project_id="p",title="Test",publish_at="2099-01-01T10:00:00+05:30",privacy="private")

