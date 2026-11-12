from types import SimpleNamespace
from unittest.mock import patch
import yaml

from tool_server_lite.llm_client_lite import LLMClientLite, get_llm_client
from utils.user_paths import runtime_env_scope


def test_image_generation_accepts_role_key_without_default_key(tmp_path):
    config = tmp_path / 'llm.yaml'
    config.write_text(yaml.safe_dump({'api_key': '', 'models': ['openai/text'], 'figure_models': [{
        'name': 'openai/gpt-image-1', 'api_key': 'image-test-key',
        'base_url': 'https://images.example/v1', 'image_api': 'images'}]}))
    client = LLMClientLite(str(config))
    response = SimpleNamespace(data=[SimpleNamespace(b64_json='aW1hZ2U=', url=None)], usage=None)
    with patch('litellm.image_generation', return_value=response) as call:
        result = client.create_image('A red square')
    assert result == 'data:image/png;base64,aW1hZ2U='
    assert call.call_args.kwargs['api_key'] == 'image-test-key'
    assert call.call_args.kwargs['api_base'] == 'https://images.example/v1'
    assert 'response_format' not in call.call_args.kwargs


def test_tool_model_cache_does_not_reuse_another_root_key(tmp_path):
    clients = []
    for name in ['first', 'second']:
        root = tmp_path / name
        (root / 'config').mkdir(parents=True)
        (root / 'config/llm_config.yaml').write_text(yaml.safe_dump({'api_key': name + '-test-key', 'models': ['openai/text']}))
        with runtime_env_scope({'MLA_USER_DATA_ROOT': str(root), 'MLA_LLM_CONFIG_PATH': str(root / 'config/llm_config.yaml')}):
            clients.append(get_llm_client())
    assert clients[0] is not clients[1]
    assert clients[0].api_key == 'first-test-key'
    assert clients[1].api_key == 'second-test-key'
