"""Duration regression tests. All model responses are local technical fixtures."""
import copy
import shutil
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app import creative, media, providers, worker
from app.config import update_settings
from app.db import SessionLocal
from app.models import Asset, Project, Usage
from app.schemas import ProjectInput, Settings
from app.duration import split_duration


def test_default_and_minimum_are_enforced_in_api(client):
    response = client.post('/api/projects', json={'prompt': 'A tea stall at sunrise'})
    assert response.status_code == 201
    assert response.json()['settings']['duration'] == 45
    for duration in (5, 10, 40, 44, 46, 125):
        assert client.post('/api/projects', json={'settings': {'duration': duration}}).status_code == 422
    for duration in (45, 60, 90, 120):
        assert client.patch('/api/projects/' + response.json()['id'], json={'settings': {'duration': duration}}).status_code == 200


def test_legacy_draft_opens_and_saves_without_losing_scenes(client):
    old = ProjectInput(prompt='Keep my original story', scenes=[{'prompt': 'Original scene'}]).model_dump()
    old['settings']['duration'] = 5
    with SessionLocal() as db:
        row = Project(payload=old)
        db.add(row); db.commit(); project_id = row.id
    opened = client.get('/api/projects/' + project_id).json()
    assert opened['settings']['duration'] == 45
    assert opened['scenes'] == old['scenes']
    saved = client.patch('/api/projects/' + project_id, json={'title': 'Updated title'})
    assert saved.status_code == 200 and saved.json()['settings']['duration'] == 45
    assert saved.json()['prompt'] == old['prompt']


def test_provider_limits_are_separate_from_total_length(monkeypatch):
    update_settings({'fal_key': 'TEST_ONLY'})
    payload = ProjectInput(prompt='A fictional scene').model_dump()
    assert providers.validate_request(payload)['durations'] == [5, 10]
    assert split_duration(45, [5, 10]) == [10, 10, 10, 10, 5]
    assert split_duration(45, [5]) == [5] * 9
    assert split_duration(120, [10]) == [10] * 12
    with pytest.raises(ValueError, match='workflow'):
        split_duration(45, [10])
    def no_network(*args, **kwargs):
        pytest.fail('A 45-second request must never reach the short-shot endpoint')
    monkeypatch.setattr(providers.httpx, 'post', no_network)
    with pytest.raises(providers.ProviderError, match='individual'):
        providers.FalProvider({'fal_key': 'TEST_ONLY'}).submit('Test', Settings().model_dump())


def test_short_storyboard_cannot_queue_complete_video(client):
    update_settings({'fal_key': 'TEST_ONLY'})
    created = client.post('/api/projects', json={'prompt': 'Test', 'scenes': [{'prompt': 'A fictional scene', 'duration': 10}]}).json()
    response = client.post('/api/projects/' + created['id'] + '/generate', json={})
    assert response.status_code == 409 and 'at least 45 seconds' in response.json()['detail']
    assert client.get('/api/jobs').json() == []
    # Individual shot generation remains available while building a storyboard.
    response = client.post('/api/projects/' + created['id'] + '/generate', json={'scene_id': created['scenes'][0]['id']})
    assert response.status_code == 202


def test_script_plan_must_match_requested_duration(monkeypatch):
    calls = []
    def plan(system, prompt):
        calls.append(prompt)
        return {'title': 'Fictional story', 'hook': 'A surprise', 'scenes': [{'prompt': 'An original fictional tea stall shot', 'duration': n} for n in [10, 10, 10, 10, 5]]}
    monkeypatch.setattr(creative, '_llm', plan)
    result = creative.create_plan(creative.PlanRequest(text='A fictional tea stall story'))
    assert sum(s['duration'] for s in result['scenes']) == 45
    assert 'Total duration: 45' in calls[0]
    assert all(s['trim_end'] == s['duration'] for s in result['scenes'])
    with pytest.raises(HTTPException) as error:
        creative.create_plan(creative.PlanRequest(text='A fictional tea stall story', duration=60))
    assert error.value.status_code == 502


def test_generation_worker_assembles_45_seconds_from_short_shots(client, monkeypatch, tmp_path):
    # Synthetic color fixtures stay in isolated test storage, never user projects.
    fixtures = {}
    for duration in (5, 10):
        path = tmp_path / f'fixture-{duration}.mp4'
        media.run([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                   'color=c=blue:size=64x64:rate=16', '-t', str(duration), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)])
        fixtures[duration] = path
    requests = []
    class FixtureProvider:
        def submit(self, prompt, options, image):
            requests.append(copy.deepcopy(options))
            assert options['duration'] in (5, 10)
            return {'provider': 'fal', 'request_id': str(len(requests)), 'duration': options['duration']}
        def poll(self, handle):
            return {'status': 'COMPLETED'}
        def download(self, handle, destination):
            shutil.copyfile(fixtures[handle['duration']], destination)
            return {'fixture_only': True}
    monkeypatch.setattr(worker, 'get_provider', lambda _: FixtureProvider())
    update_settings({'fal_key': 'TEST_ONLY'})
    created = client.post('/api/projects', json={'prompt': 'Technical pipeline test', 'settings': {'resolution': '480p', 'aspect_ratio': '1:1', 'subtitles': False}}).json()
    response = client.post('/api/projects/' + created['id'] + '/generate', json={})
    assert response.status_code == 202, response.text
    job_id = worker.claim_job('duration-test')
    worker.process_job(job_id, 'duration-test')
    result = client.get('/api/jobs/' + job_id).json()
    assert result['status'] == 'completed', result
    saved = client.get('/api/projects/' + created['id']).json()
    assert [s['duration'] for s in saved['scenes']] == [10, 10, 10, 10, 5]
    assert len({s['prompt'] for s in saved['scenes']}) == 5
    assert [r['duration'] for r in requests] == [10, 10, 10, 10, 5]
    with SessionLocal() as db:
        output = db.get(Asset, saved['output_asset_id'])
        video = next(s for s in output.meta['streams'] if s['codec_type'] == 'video')
        assert 45 <= float(video['duration']) < 45.2
        assert len(list(db.scalars(select(Usage)))) == 5
    with pytest.raises(ValueError, match='at least 45'):
        media.assemble([fixtures[5]], tmp_path / 'must-not-export.mp4', [{'duration': 5}],
                       {'resolution': '480p', 'aspect_ratio': '1:1', 'fps': 16, 'minimum_duration': 45})
    assert not (tmp_path / 'must-not-export.mp4').exists()
