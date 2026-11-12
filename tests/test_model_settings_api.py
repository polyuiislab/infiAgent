from fastapi.testclient import TestClient
import yaml
from infiagent.runtime.server import create_app


def test_all_model_roles_save_separate_connections_and_keep_keys_private(tmp_path):
    app = create_app(tmp_path)
    body = {'base_url': 'https://example.com/v1', 'api_key': 'default-test-key', 'roles': {
        'main': {'model': 'openai/text', 'inherit': True},
        'thinking': {'model': 'openai/think', 'inherit': True},
        'compressor': {'model': 'openai/compact', 'inherit': True},
        'read_figure': {'model': 'openai/vision', 'inherit': False, 'base_url': 'https://vision.example/v1', 'api_key': 'vision-test-key'},
        'figure': {'model': 'openai/image', 'inherit': False, 'base_url': 'https://images.example/v1', 'api_key': 'image-test-key'},
    }}
    with TestClient(app) as client:
        response = client.put('/api/model-settings', json=body)
        assert response.status_code == 200, response.text
        public = client.get('/api/model-settings').json()
        assert public['roles']['read_figure']['has_api_key']
        assert 'vision-test-key' not in str(public)
        saved = yaml.safe_load((tmp_path / 'config/llm_config.yaml').read_text())
        assert saved['read_figure_models'][0]['name'] == 'openai/vision'
        assert saved['figure_models'][0]['api_key'] == 'image-test-key'
        assert saved['thinking_models'][0]['name'] == 'openai/think'
        body['base_url'] = 'https://different.example/v1'
        body['api_key'] = ''
        assert client.put('/api/model-settings', json=body).status_code == 400
        assert yaml.safe_load((tmp_path / 'config/llm_config.yaml').read_text()) == saved
