"""Developer runtime tests; all resources live under pytest's temporary root."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from infiagent.runtime.server import create_app
from utils.atomic_state import write_json


def test_runtime_health_console_and_config(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/health').json()['version'] == '3.13.0'
        assert 'InfiAgent' in client.get('/').text
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/api/agents').json()['items']
        assert client.put('/api/config/model', json={'content': 'broken: ['}).status_code == 400
        assert client.put('/api/config/model', json={'content': 'models: [openai/mock]\napi_key: local-only\n'}).status_code == 200
        assert 'local-only' in client.get('/api/config/model').json()['content']


def test_workspace_file_api_confines_paths_and_uploads(tmp_path):
    app = create_app(tmp_path)
    runtime = app.state.runtime
    task = tmp_path / 'tasks' / 'demo'
    (task / '.sdk_runtime').mkdir(parents=True)
    write_json(runtime.sessions / 'demo.json', {'id': 'demo', 'status': 'success'})
    with TestClient(app) as client:
        assert client.post('/api/workspaces', json={'id': 'shared'}).status_code == 200
        assert client.post('/api/workspaces', json={'id': '../escape'}).status_code == 400
        assert client.put('/api/sessions/demo/file?path=note.txt', content=b'hello').status_code == 200
        assert client.get('/api/sessions/demo/file?path=note.txt').content == b'hello'
        assert client.get('/api/sessions/demo/files').json()['items'][0]['name'] == 'note.txt'
        assert client.put('/api/sessions/demo/file?path=../outside', content=b'bad').status_code == 400
        assert client.get('/api/sessions/demo/file?path=.sdk_runtime/state.json').status_code == 400


def test_event_replay_preserves_sequence_and_cursor(tmp_path):
    app = create_app(tmp_path)
    runtime = app.state.runtime
    state = tmp_path / 'tasks/demo/.sdk_runtime'
    state.mkdir(parents=True)
    write_json(runtime.sessions / 'demo.json', {'id': 'demo', 'status': 'success'})
    runtime.append('demo', {'type': 'message', 'content': 'one'})
    runtime.append('demo', {'type': 'result', 'result': {'status': 'success', 'output': 'two'}})
    with TestClient(app) as client:
        text = client.get('/api/sessions/demo/events?after=1').text
        assert 'one' not in text and 'two' in text and 'id: 2' in text


def test_optional_api_token_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv('INFIAGENT_API_TOKEN', 'test-only-token')
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/sessions').status_code == 401
        assert client.get('/api/sessions', headers={'Authorization': 'Bearer test-only-token'}).status_code == 200


def test_console_rejects_foreign_origin_writes_and_rebound_hosts(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/workspaces', json={'id': 'bad'}, headers={'Origin': 'https://foreign.example'}).status_code == 403
        assert client.get('/api/config/model', headers={'Host': 'foreign.example'}).status_code == 400
        assert client.post('/api/workspaces', json={'id': 'ok'}, headers={'Origin': 'http://testserver'}).status_code == 200
