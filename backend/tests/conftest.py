"""All API tests use isolated D-drive storage; no real account configuration is read."""
import os, uuid
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
TEST_DATA = ROOT / ".tmp" / ("pytest-api-" + uuid.uuid4().hex)
TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["STORYORBIT_DATA_DIR"] = str(TEST_DATA)
os.environ["DATABASE_URL"] = "sqlite:///" + (TEST_DATA/"test.db").as_posix()
for key in ("FAL_KEY","LLM_API_KEY","TTS_API_KEY","COMFYUI_URL","COMFYUI_WORKFLOW_PATH","STORYORBIT_API_TOKEN","STORYORBIT_PUBLIC"):
    os.environ.pop(key,None)

from app.db import Base, engine
from app.main import app, _buckets
from app.config import DATA_DIR
from app.worker import _STOP

@pytest.fixture(autouse=True)
def isolated_database():
    assert DATA_DIR == TEST_DATA
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    (DATA_DIR/"settings.json").unlink(missing_ok=True)
    _buckets.clear()
    _STOP.clear()
    yield
    engine.dispose()
    (DATA_DIR/"settings.json").unlink(missing_ok=True)

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    with TestClient(app) as value:
        yield value

