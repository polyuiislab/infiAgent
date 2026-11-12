"""Five independent model roles with shared or role-specific connections."""
from __future__ import annotations

import os
import uuid
import functools
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter

SLOTS = {'main': 'models', 'thinking': 'thinking_models', 'compressor': 'compressor_models',
         'read_figure': 'read_figure_models', 'figure': 'figure_models'}


def name_of(entry):
    return entry if isinstance(entry, str) else str(entry.get('name') or entry.get('model') or '')


def install_model_settings(app, runtime):
    router = APIRouter(prefix='/api/model-settings')
    path = runtime.root / 'config' / 'llm_config.yaml'

    def read():
        value = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(value, dict):
            raise ValueError('Model configuration must be a mapping')
        return value

    @router.get('')
    def get():
        config = read()
        roles = {}
        for role, key in SLOTS.items():
            entries = config.get(key) or []
            entry = entries[0] if entries else ''
            details = entry if isinstance(entry, dict) else {}
            roles[role] = {'model': name_of(entry), 'base_url': details.get('base_url', ''),
                           'has_api_key': bool(details.get('api_key')),
                           'image_api': details.get('image_api', 'auto') if role == 'figure' else None,
                           'inherit': not bool(details.get('base_url') or details.get('api_key'))}
        return {'base_url': config.get('base_url', ''), 'has_api_key': bool(config.get('api_key')),
                'roles': roles, 'multimodal': bool(config.get('multimodal', False)),
                'compressor_multimodal': bool(config.get('compressor_multimodal', False))}

    def url(value):
        value = str(value or '').strip()
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {'https', 'http'} or not parts.netloc or parts.username or parts.password:
                raise ValueError('API address must be an HTTP(S) URL without embedded credentials')
        return value

    def connection(old, updates):
        base = url(updates.get('base_url', ''))
        key = str(updates.get('api_key') or '').strip()
        if old.get('api_key') and old.get('base_url', '') != base and not key and not updates.get('clear_key'):
            raise ValueError('API address changed: enter the key for the new address, or clear the stored key')
        result = {'base_url': base, 'api_key': '' if updates.get('clear_key') else key or old.get('api_key', '')}
        return result

    def configuration_writer(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            from utils.configuration_lock import configuration_access
            with runtime.lock, configuration_access(runtime.root, write=True):
                return fn(*args, **kwargs)
        return wrapped

    @router.put('')
    @configuration_writer
    def save(body: dict):
        config = read()
        config.update(connection(config, body))
        roles = body.get('roles') or {}
        if not isinstance(roles, dict):
            raise ValueError('roles must be an object')
        seen = {}
        for role, key in SLOTS.items():
            if role not in roles:
                continue
            update = roles[role]
            if not isinstance(update, dict):
                raise ValueError('Each model role must be an object')
            model = str(update.get('model') or '').strip()
            if not model:
                if role == 'main':
                    raise ValueError('Configure a main execution model')
                config[key] = []
                continue
            # Native provider prefixes are accepted verbatim. Plain names use
            # the OpenAI-compatible protocol, including local model servers.
            prefixes = ('openai/', 'openrouter/', 'anthropic/', 'gemini/', 'google/', 'ollama/', 'ollama_chat/', 'azure/', 'bedrock/')
            if not model.startswith(prefixes):
                model = 'openai/' + model
            previous = config.get(key) or []
            first = previous[0] if previous else {}
            details = dict(first) if isinstance(first, dict) else {}
            old_name = name_of(first)
            if old_name != model:
                # A new model must not inherit deployment-specific endpoints.
                details.pop('deployments', None)
            details['name'] = model
            if role == 'figure':
                protocol = update.get('image_api', details.get('image_api', 'auto'))
                if protocol not in {'auto', 'chat', 'images'}:
                    raise ValueError('Invalid image-generation API type')
                details['image_api'] = protocol
            if update.get('inherit', True):
                details.pop('base_url', None)
                details.pop('api_key', None)
            else:
                details.update(connection(details, update))
            effective = (details.get('base_url', config.get('base_url', '')),
                         details.get('api_key', config.get('api_key', '')))
            if model in seen and seen[model] != effective:
                raise ValueError('The same model name must use the same connection in each role')
            seen[model] = effective
            config[key] = [details] + [item for item in previous[1:] if name_of(item) != model]
        config['multimodal'] = bool(body.get('multimodal', config.get('multimodal', False)))
        config['compressor_multimodal'] = bool(body.get('compressor_multimodal', config.get('compressor_multimodal', False)))
        temporary = path.with_name('.models-' + uuid.uuid4().hex + '.yaml')
        try:
            temporary.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding='utf-8')
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {'status': 'saved'}

    app.include_router(router)
