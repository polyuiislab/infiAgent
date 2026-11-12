import io
import zipfile
from fastapi.testclient import TestClient
from infiagent.runtime.server import create_app


def test_yellow_console_ensure_workspace_upload_and_history(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        page = client.get('/').text
        assert 'Usage Panel' in page and 'model_config.js' in page
        result = client.post('/api/v1/tasks/ensure', json={'task_id': 'console-demo', 'agent_system': 'OpenCowork',
            'agent_name': 'alpha_agent', 'workspace': {'create': {'name': 'Demo workspace'}}})
        assert result.status_code == 200, result.text
        assert result.json()['workspace']['name'] == 'Demo workspace'
        assert client.get('/api/v1/tasks').json()['items'][0]['task_id'] == 'console-demo'
        uploaded = client.post('/api/v1/tasks/upload-files', data={'task_id': 'console-demo', 'dest_subdir': '.upload'},
                               files={'files': ('sample.txt', b'hello', 'text/plain')})
        assert uploaded.status_code == 200, uploaded.text
        assert client.get('/api/v1/tasks/artifact', params={'task_id': 'console-demo', 'path': '.upload/sample.txt'}).content == b'hello'
        assert client.get('/api/v1/tasks/history?task_id=console-demo').json()['items'] == []
        assert client.get('/api/v1/tasks/artifact?task_id=console-demo&path=../../outside').status_code == 400


def test_configuration_import_rejects_traversal_and_preserves_tasks(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.post('/api/v1/tasks/ensure', json={'task_id': 'keep'})
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('config/../../escape.txt', 'bad')
        result = client.post('/api/v1/runtime/upload-user-root', data={'mode': 'merge_overwrite'},
                             files={'archive': ('test.zip', archive.getvalue(), 'application/zip')})
        assert result.status_code == 400
        assert client.get('/api/v1/tasks/meta?task_id=keep').json()['exists']


def test_queue_without_resume_does_not_start_a_model_worker(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        client.post('/api/v1/tasks/ensure', json={'task_id': 'queued'})
        response = client.post('/api/v1/tasks/add-message', json={
            'task_id': 'queued', 'new_message': 'Do this later', 'resume_if_needed': False})
        assert response.status_code == 200
        assert response.json()['queued']
        assert app.state.runtime.processes == {}
        assert client.get('/api/v1/tasks/meta?task_id=queued').json()['queued_messages'] == ['Do this later']
