"""Local runtime: sessions, SSE, workspaces, files and model configuration."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import ipaddress
import functools
from urllib.parse import urlsplit
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from utils.atomic_state import read_json, write_json
from utils.user_paths import runtime_env_scope
from utils import workspaces, interactions

ID = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


class Message(BaseModel):
    message: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None
    agent_system: str = 'OpenCowork'
    agent_name: str = 'alpha_agent'
    workspace_id: str | None = None


class Workspace(BaseModel):
    id: str
    title: str = ''
    concurrency: str = 'serial'


class SessionCreate(BaseModel):
    agent_system: str = 'OpenCowork'
    agent_name: str = 'alpha_agent'
    workspace_id: str | None = None


class Answer(BaseModel):
    response: str


class ConfigText(BaseModel):
    content: str = Field(max_length=1000000)


class LocalRuntime:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.sessions = self.root / 'runtime' / 'sessions'
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.processes = {}
        self.sequence = {}
        self.maximum = int(os.environ.get('INFIAGENT_MAX_RUNS', '2'))
        if self.maximum < 1:
            raise ValueError('INFIAGENT_MAX_RUNS must be positive')
        with self.scope():
            from utils.user_paths import apply_runtime_env_defaults
            apply_runtime_env_defaults()
        for path in self.sessions.glob('*.json'):
            record = read_json(path)
            if record.get('status') in {'running', 'stopping'}:
                write_json(path, {**record, 'status': 'interrupted'})

    def scope(self):
        return runtime_env_scope({'MLA_USER_DATA_ROOT': str(self.root),
                                  'MLA_SKILLS_LIBRARY_DIR': str(self.root / 'skills')})

    def session(self, sid):
        if not ID.fullmatch(sid):
            raise HTTPException(400, 'Invalid session ID')
        record = read_json(self.sessions / (sid + '.json'))
        if not record:
            raise HTTPException(404, 'Session not found')
        return record

    def task(self, sid):
        self.session(sid)
        task = self.root / 'tasks' / sid
        if task.is_symlink() or task.resolve().parent != (self.root / 'tasks').resolve():
            raise HTTPException(400, 'Invalid task directory')
        return task

    def events_file(self, sid):
        return self.task(sid) / '.sdk_runtime' / 'http-events.ndjson'

    def append(self, sid, value):
        with self.lock:
            path = self.events_file(sid)
            if sid not in self.sequence:
                self.sequence[sid] = 0
                if path.exists():
                    for line in path.read_text(encoding='utf-8').splitlines():
                        try:
                            self.sequence[sid] = max(self.sequence[sid], int(json.loads(line)['id']))
                        except (ValueError, KeyError):
                            continue
            self.sequence[sid] += 1
            event = {'id': self.sequence[sid], 'timestamp': time.time(),
                     'run_id': self.session(sid).get('run_id', ''), **value}
            with path.open('a', encoding='utf-8') as out:
                out.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
                out.flush()
            return event

    def start(self, message, sdk_overrides=None):
        with self.lock:
            sid = message.session_id or uuid.uuid4().hex
            if not ID.fullmatch(sid):
                raise HTTPException(400, 'Invalid session ID')
            previous = self.session(sid) if message.session_id else None
            if sid in self.processes and self.processes[sid].poll() is None:
                with self.scope():
                    from core.hierarchy_manager import get_hierarchy_manager
                    get_hierarchy_manager(str(self.task(sid))).append_instruction(message.message, source='user')
                self.append(sid, {'type': 'message', 'role': 'user', 'content': message.message})
                return previous
            if sid in self.processes:
                raise HTTPException(409, 'Previous run is finalizing; retry shortly')
            if sum(p.poll() is None for p in self.processes.values()) >= self.maximum:
                raise HTTPException(429, 'Runtime is busy; wait for an active run to finish')
            if previous and message.workspace_id not in (None, previous.get('workspace_id')):
                raise HTTPException(409, 'Create a new session to use a different workspace')
            queued = list((previous or {}).get('queued_messages') or [])
            effective_message = message.message
            if queued:
                combined = '\n\n'.join(queued)
                effective_message = combined if message.message in {combined, queued[-1]} else combined + '\n\n' + message.message
            system = previous['agent_system'] if previous else message.agent_system
            agent = previous['agent_name'] if previous else message.agent_name
            ws = previous.get('workspace_id') if previous else message.workspace_id
            if not ID.fullmatch(system) or not ID.fullmatch(agent):
                raise HTTPException(400, 'Invalid agent name')
            with self.scope():
                from utils.config_loader import ConfigLoader
                loader = ConfigLoader(system, agent_library_root=str(self.root))
                if loader.get_tool_config(agent).get('type') != 'llm_call_agent':
                    raise ValueError('Select an agent entry point')
                if ws and ws not in {x['id'] for x in workspaces.list_workspaces()}:
                    raise ValueError('Workspace not found')
            task = self.root / 'tasks' / sid
            state = task / '.sdk_runtime'
            state.mkdir(parents=True, exist_ok=True)
            if ws:
                with self.scope():
                    workspaces.attach(task, ws)
            request = {'message': effective_message, 'user_root': str(self.root), 'task_path': str(task),
                       'agent_system': system, 'agent_name': agent, 'workspace_id': ws,
                       'sdk_overrides': sdk_overrides or {},
                       'resume_input': (previous or {}).get('run_input'),
                       'previous_message': (previous or {}).get('last_message'),
                       'resume': bool(previous and previous['status'] in {'interrupted', 'cancelled', 'stopped', 'error'})}
            request_file = state / 'http-request.json'
            write_json(request_file, request)
            title = previous['title'] if previous and previous.get('status') != 'ready' else message.message[:60]
            record = {**(previous or {}), 'id': sid, 'title': title,
                      'run_id': uuid.uuid4().hex,
                      'run_input': ((previous or {}).get('run_input') or (previous or {}).get('last_message') or effective_message) if request['resume'] else effective_message,
                      'queued_messages': [],
                      'agent_system': system, 'agent_name': agent, 'workspace_id': ws,
                      'status': 'running', 'updated_at': time.time(), 'last_message': message.message}
            write_json(self.sessions / (sid + '.json'), record)
            env = dict(os.environ)
            env['MLA_USER_DATA_ROOT'] = str(self.root)
            env['MLA_SKILLS_LIBRARY_DIR'] = str(self.root / 'skills')
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
            env['PYTHONUTF8'] = '1'
            env['PYTHONIOENCODING'] = 'utf-8'
            env.pop('INFIAGENT_API_TOKEN', None)
            log = (state / 'worker.log').open('a', encoding='utf-8')
            try:
                proc = subprocess.Popen([sys.executable, '-m', 'infiagent.runtime.worker', str(request_file)],
                    cwd=task, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding='utf-8', bufsize=1, start_new_session=(os.name != 'nt'))
            except Exception:
                write_json(self.sessions / (sid + '.json'), {**record, 'status': 'error'})
                raise
            finally:
                log.close()
            self.processes[sid] = proc
            self.append(sid, {'type': 'message', 'role': 'user', 'content': message.message})
            threading.Thread(target=self._read_worker, args=(sid, proc), daemon=True).start()
            return record

    def new_session(self, body, sid=None):
        with self.lock:
            if sid and (self.sessions / (sid + '.json')).exists():
                return self.session(sid)
            return self._new_session(body, sid)

    def _new_session(self, body, sid=None):
        if not ID.fullmatch(body.agent_system) or not ID.fullmatch(body.agent_name):
            raise ValueError('Invalid agent name')
        sid = sid or uuid.uuid4().hex
        if not ID.fullmatch(sid):
            raise ValueError('Invalid task ID')
        with self.scope():
            from utils.config_loader import ConfigLoader
            ConfigLoader(body.agent_system, agent_library_root=str(self.root)).get_tool_config(body.agent_name)
            if body.workspace_id and body.workspace_id not in {x['id'] for x in workspaces.list_workspaces()}:
                raise ValueError('Workspace not found')
            task = self.root / 'tasks' / sid
            (task / '.sdk_runtime').mkdir(parents=True)
            if body.workspace_id:
                workspaces.attach(task, body.workspace_id)
        record = {'id': sid, 'title': '新会话', 'status': 'ready', 'updated_at': time.time(),
                  'agent_system': body.agent_system, 'agent_name': body.agent_name, 'workspace_id': body.workspace_id}
        write_json(self.sessions / (sid + '.json'), record)
        return record

    def _read_worker(self, sid, proc):
        result = None
        try:
            for line in proc.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                self.append(sid, value)
                if value.get('type') == 'result':
                    result = value['result']
            proc.wait()
        finally:
            with self.lock:
                status = (result or {}).get('status', 'interrupted')
                record = self.session(sid)
                write_json(self.sessions / (sid + '.json'), {**record, 'status': status,
                           'result': result, 'updated_at': time.time()})
                self.append(sid, {'type': 'finished', 'status': status})
                if self.processes.get(sid) is proc:
                    self.processes.pop(sid, None)

    def stop(self, sid):
        with self.lock:
            record = self.session(sid)
            proc = self.processes.get(sid)
            if proc is None or proc.poll() is not None:
                return {'status': 'not_running'}
            try:
                proc.stdin.write('{"action":"stop"}\n')
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            write_json(self.sessions / (sid + '.json'), {**record, 'status': 'stopping'})
            timer = threading.Timer(30, self.terminate, args=(proc,))
            timer.daemon = True
            timer.start()
            return {'status': 'stopping'}

    @staticmethod
    def terminate(proc):
        if proc.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    def close(self):
        for sid in list(self.processes):
            self.stop(sid)
        for proc in list(self.processes.values()):
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.terminate(proc)


def create_app(user_root=None):
    root = user_root or os.environ.get('INFIAGENT_USER_ROOT') or os.environ.get('MLA_USER_DATA_ROOT') or str(Path.home() / 'mla_v3')
    runtime = LocalRuntime(root)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        runtime.close()

    app = FastAPI(title='InfiAgent Runtime', version='3.13.0', lifespan=lifespan)
    app.state.runtime = runtime

    @app.middleware('http')
    async def token_guard(request, call_next):
        from fastapi.responses import JSONResponse
        if request.url.path.startswith('/api/'):
            host = urlsplit('//' + request.headers.get('host', '')).hostname or ''
            allowed = {'localhost'} | {h.strip().lower() for h in os.environ.get('INFIAGENT_ALLOWED_HOSTS', '').split(',') if h.strip()}
            try:
                ipaddress.ip_address(host)
                valid_host = True
            except ValueError:
                valid_host = host.lower() in allowed
            if not valid_host:
                return JSONResponse({'detail': 'Unrecognized runtime host'}, status_code=400)
            origin = request.headers.get('origin')
            if origin and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                if urlsplit(origin).netloc.lower() != request.headers.get('host', '').lower():
                    return JSONResponse({'detail': 'Cross-origin writes are not allowed'}, status_code=403)
        token = os.environ.get('INFIAGENT_API_TOKEN', '')
        if token and request.url.path.startswith('/api/'):
            presented = request.headers.get('authorization', '')
            if not hmac.compare_digest(presented.encode(), ('Bearer ' + token).encode()):
                return JSONResponse({'detail': 'API token required'}, status_code=401)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(BlockingIOError)
    async def busy_file(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': '当前任务正在使用这些文件或配置，请等待任务结束或先停止任务。'}, status_code=409)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'version': '3.13.0'}

    @app.get('/api/runtime')
    def info():
        return {'version': '3.13.0', 'user_root': str(runtime.root), 'max_runs': runtime.maximum}

    @app.get('/api/agents')
    def agents():
        items = []
        for directory in sorted((runtime.root / 'agent_library').glob('*')):
            if not directory.is_dir() or directory.is_symlink():
                continue
            with runtime.scope():
                from utils.config_loader import ConfigLoader
                try:
                    loader = ConfigLoader(directory.name, agent_library_root=str(runtime.root))
                    for name, item in loader.all_tools.items():
                        if item.get('type') == 'llm_call_agent':
                            items.append({'system': directory.name, 'name': name})
                except Exception:
                    continue
        return {'items': items}

    @app.get('/api/sessions')
    def sessions():
        return {'items': sorted([read_json(p) for p in runtime.sessions.glob('*.json')],
                                key=lambda x: x.get('updated_at', 0), reverse=True)}

    @app.post('/api/sessions', status_code=201)
    def create_session(body: SessionCreate):
        return runtime.new_session(body)

    @app.post('/api/messages', status_code=202)
    def send(message: Message):
        return runtime.start(message)

    @app.post('/api/sessions/{sid}/stop')
    def stop(sid: str):
        return runtime.stop(sid)

    @app.get('/api/sessions/{sid}/events')
    async def events(sid: str, after: int = 0):
        path = runtime.events_file(sid)
        async def stream():
            offset = 0
            while True:
                if path.exists():
                    with path.open(encoding='utf-8') as source:
                        source.seek(offset)
                        while True:
                            start = source.tell()
                            line = source.readline()
                            if not line or not line.endswith('\n'):
                                offset = start
                                break
                            offset = source.tell()
                            value = json.loads(line)
                            if value['id'] > after:
                                yield f'id: {value["id"]}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n'
                if runtime.session(sid)['status'] not in {'running', 'stopping'}:
                    break
                yield ': keepalive\n\n'
                await asyncio.sleep(.3)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    @app.get('/api/workspaces')
    def workspace_list():
        with runtime.scope():
            return {'items': workspaces.list_workspaces()}

    @app.post('/api/workspaces')
    def workspace_create(body: Workspace):
        with runtime.scope():
            return workspaces.create(body.id, body.title, body.concurrency)

    @app.get('/api/sessions/{sid}/files')
    def files(sid: str, path: str = ''):
        with runtime.scope():
            task = runtime.task(sid)
            directory = workspaces.resolve_file(task, path)
            root = workspaces.files_root(task)
            if not directory.is_dir():
                raise HTTPException(404, 'Directory not found')
            return {'path': path, 'items': [{'name': p.name, 'path': str(p.relative_to(root)),
                    'directory': p.is_dir(), 'size': p.stat().st_size if p.is_file() else 0}
                    for p in sorted(directory.iterdir()) if not p.is_symlink() and p.name != '.sdk_runtime']}

    @app.get('/api/sessions/{sid}/file')
    def download(sid: str, path: str):
        with runtime.scope():
            file = workspaces.resolve_file(runtime.task(sid), path)
        if not file.is_file():
            raise HTTPException(404, 'File not found')
        return FileResponse(file, filename=file.name)

    @app.put('/api/sessions/{sid}/file')
    async def upload(sid: str, path: str, request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 20 * 1024 * 1024:
                raise HTTPException(413, 'File limit is 20 MiB')
        with runtime.scope():
            task = runtime.task(sid)
            file = workspaces.resolve_file(task, path)
            with workspaces.file_access(task, blocking=False):
                file.parent.mkdir(parents=True, exist_ok=True)
                temporary = file.with_name('.' + file.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_bytes(body)
                    os.replace(temporary, file)
                finally:
                    temporary.unlink(missing_ok=True)
        return {'status': 'saved', 'path': path}

    @app.get('/api/sessions/{sid}/interactions')
    def waiting(sid: str):
        return {'items': [r for _, r in interactions.records(runtime.task(sid)) if r['status'] == 'waiting']}

    @app.post('/api/sessions/{sid}/interactions/{occurrence}/answer')
    def answer(sid: str, occurrence: str, body: Answer):
        return interactions.answer(occurrence, body.response, runtime.task(sid))

    @app.get('/api/config/model')
    def config():
        path = runtime.root / 'config' / 'llm_config.yaml'
        return {'content': path.read_text(encoding='utf-8') if path.exists() else ''}

    def configuration_writer(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            from utils.configuration_lock import configuration_access
            with runtime.lock, configuration_access(runtime.root, write=True):
                return fn(*args, **kwargs)
        return wrapped

    @app.put('/api/config/model')
    @configuration_writer
    def save_config(body: ConfigText):
        try:
            value = yaml.safe_load(body.content)
        except yaml.YAMLError as exc:
            raise ValueError('Invalid YAML configuration') from exc
        if not isinstance(value, dict):
            raise ValueError('Model configuration must be a YAML mapping')
        path = runtime.root / 'config' / 'llm_config.yaml'
        temporary = path.with_name('.model-' + uuid.uuid4().hex)
        try:
            temporary.write_text(body.content, encoding='utf-8')
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {'status': 'saved'}

    assets = Path(__file__).parent / 'static'
    from .console_api import install_console_api
    install_console_api(app, runtime)
    from .model_settings import install_model_settings
    install_model_settings(app, runtime)
    app.mount('/static', StaticFiles(directory=assets), name='static')

    @app.get('/')
    @app.get('/index.html')
    def index():
        return FileResponse(assets / 'index.html', headers={'Cache-Control': 'no-cache'})

    @app.get('/usage.html')
    def usage_page():
        return FileResponse(assets / 'usage.html', headers={'Cache-Control': 'no-cache'})

    return app


def main():
    parser = argparse.ArgumentParser(description='Run the InfiAgent developer console and HTTP API')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4260)
    parser.add_argument('--user-root', default=None)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.user_root), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
