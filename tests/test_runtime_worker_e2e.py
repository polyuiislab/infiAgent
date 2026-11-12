"""HTTP API → separate worker → SDK → local OpenAI-compatible model → SSE."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml
from fastapi.testclient import TestClient
from infiagent.runtime.server import create_app


def test_runtime_runs_real_worker_against_local_model(tmp_path):
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model'] == 'mock'
            arguments = json.dumps({'status': 'success', 'output': 'worker round trip passed'})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            events = [
                {'id': 'local-test', 'object': 'chat.completion.chunk', 'model': 'mock', 'created': 1,
                 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [{'index': 0,
                    'id': 'local-final', 'type': 'function', 'function': {'name': 'final_output', 'arguments': arguments}}]}, 'finish_reason': None}]},
                {'id': 'local-test', 'object': 'chat.completion.chunk', 'model': 'mock', 'created': 1,
                 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}],
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}},
            ]
            for event in events:
                self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        app = create_app(tmp_path)
        config = tmp_path / 'config/llm_config.yaml'
        config.write_text(yaml.safe_dump({'api_key': 'local-test-only',
            'base_url': f'http://127.0.0.1:{model.server_port}/v1', 'models': ['openai/mock']}))
        system = tmp_path / 'agent_library/worker_demo'
        system.mkdir()
        (system / 'general_prompts.yaml').write_text('general_prompts: {}\n')
        (system / 'level_0_tools.yaml').write_text(yaml.safe_dump({'tools': {'final_output': {
            'type': 'tool_call_agent', 'level': 0, 'parameters': {'type': 'object', 'properties': {
                'output': {'type': 'string'}, 'status': {'type': 'string'}}, 'required': ['output']}}}}))
        (system / 'level_3_agents.yaml').write_text(yaml.safe_dump({'tools': {'alpha_agent': {
            'type': 'llm_call_agent', 'level': 3, 'available_tools': ['final_output'],
            'reasoning_mode': 'react_lite', 'max_turns': 2}}}))
        with TestClient(app) as client:
            response = client.post('/api/messages', json={'message': 'Say hello', 'agent_system': 'worker_demo'})
            assert response.status_code == 202, response.text
            sid = response.json()['id']
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                record = client.get('/api/sessions').json()['items'][0]
                if record['status'] not in {'running', 'stopping'}:
                    break
                time.sleep(.1)
            assert record['status'] == 'success', (record, (tmp_path / f'tasks/{sid}/.sdk_runtime/worker.log').read_text())
            assert record['result']['output'] == 'worker round trip passed'
            assert record['result']['usage']['total_tokens'] == 15
            replay = client.get(f'/api/sessions/{sid}/events').text
            assert 'worker round trip passed' in replay
            assert '"type": "finished"' in replay
    finally:
        model.shutdown()
        model.server_close()
