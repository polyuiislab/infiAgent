"""HTTP contract used by the existing InfiAgent developer console."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import time
import uuid
import io
import re
import zipfile
import stat
import functools
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from utils.atomic_state import read_json, write_json
from utils import interactions, workspaces


def install_console_api(app, runtime):
    from infiagent.runtime.server import Message, SessionCreate, ID
    router = APIRouter(prefix='/api/v1')

    def synchronized(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with runtime.lock:
                return fn(*args, **kwargs)
        return wrapped

    def sid_of(value):
        text = str(value or '').strip()
        path = Path(text)
        if path.is_absolute():
            if path.resolve().parent != (runtime.root / 'tasks').resolve():
                raise HTTPException(400, 'Task must be under user_root/tasks')
            text = path.name
        if not ID.fullmatch(text):
            raise HTTPException(400, 'Invalid task ID')
        return text

    def workspace_info(identifier):
        if not identifier:
            return None
        with runtime.scope():
            ws = next((w for w in workspaces.list_workspaces() if w['id'] == identifier), None)
        return None if ws is None else {**ws, 'workspace_id': ws['id'], 'name': ws['title'],
                                        'workspace_path': ws['path'], 'concurrency': ws.get('concurrency', 'serial')}

    def task_info(record):
        return {**record, 'task_id': record['id'], 'conversation_name': record.get('title', record['id']),
                'updated_at': datetime.fromtimestamp(record.get('updated_at', 0)).isoformat(timespec='seconds'),
                'running': record.get('status') in {'running', 'stopping'},
                'workspace': workspace_info(record.get('workspace_id')), 'exists': True}

    @synchronized
    def ensure(body):
        sid = sid_of(body.get('task_id') or uuid.uuid4().hex)
        if (runtime.sessions / (sid + '.json')).exists():
            return runtime.session(sid)
        choice = body.get('workspace')
        ws = choice if isinstance(choice, str) else None
        if isinstance(choice, dict) and 'create' in choice:
            with runtime.scope():
                spec = choice['create'] or {}
                ws = 'ws_' + uuid.uuid4().hex[:16]
                workspaces.create(ws, spec.get('name') or body.get('conversation_name') or sid,
                                  spec.get('concurrency', 'serial'))
        record = runtime.new_session(SessionCreate(agent_system=body.get('agent_system') or 'OpenCowork',
            agent_name=body.get('agent_name') or 'alpha_agent', workspace_id=ws), sid=sid)
        record['title'] = str(body.get('conversation_name') or sid)
        write_json(runtime.sessions / (sid + '.json'), record)
        return record

    def file_path(sid, relative, area='business'):
        task = runtime.task(sid)
        with runtime.scope():
            if area == 'session':
                path = (task / relative).resolve()
                if not path.is_relative_to(task.resolve()):
                    raise ValueError('Path is outside the session')
                return path, task
            return workspaces.resolve_file(task, relative), workspaces.files_root(task)

    @router.get('/runtime')
    def info():
        systems = [{'name': p.name} for p in sorted((runtime.root / 'agent_library').iterdir())
                   if p.is_dir() and not p.is_symlink()]
        return {'infiagent_version': '3.13.0', 'active_user_root': str(runtime.root),
                'tasks_root': str(runtime.root / 'tasks'), 'agent_systems': systems, 'system_add_sources': {}}

    @router.get('/agents')
    def agents(agent_system: str):
        if not ID.fullmatch(agent_system):
            raise ValueError('Invalid agent system')
        with runtime.scope():
            from utils.config_loader import ConfigLoader
            loader = ConfigLoader(agent_system, agent_library_root=str(runtime.root))
            return {'agent_system': agent_system, 'agents': [{'name': name, 'description': cfg.get('description', '')}
                    for name, cfg in loader.all_tools.items() if cfg.get('type') == 'llm_call_agent']}

    @router.get('/tasks')
    def tasks(limit: int = 200, cursor: str = ''):
        rows = sorted([read_json(p) for p in runtime.sessions.glob('*.json')],
                      key=lambda r: r.get('updated_at', 0), reverse=True)
        offset = int(cursor or '0')
        if offset < 0:
            raise ValueError('Invalid cursor')
        limit = min(500, max(1, limit))
        return {'items': [task_info(r) for r in rows[offset:offset + limit]],
                'next_cursor': str(offset + limit) if offset + limit < len(rows) else None}

    @router.post('/tasks/ensure')
    def ensure_route(body: dict):
        return task_info(ensure(body))

    @router.get('/tasks/meta')
    def meta(task_id: str):
        sid = sid_of(task_id)
        if not (runtime.sessions / (sid + '.json')).exists():
            return {'task_id': sid, 'exists': False}
        return task_info(runtime.session(sid))

    @router.get('/workspaces')
    def workspace_list():
        with runtime.scope():
            rows = workspaces.list_workspaces()
        return {'items': [workspace_info(w['id']) for w in rows]}

    def usage_query(request):
        from utils.usage_metrics import parse_range, load_pricing
        from utils.usage_ledger import UsageLedger
        query = request.query_params
        first, last = parse_range(query.get('from', ''), query.get('to', ''))
        filters = {key: query[key] for key in ('agent_system', 'agent_name', 'model', 'purpose', 'model_alias') if query.get(key)}
        if query.get('task_id'):
            filters['task_key'] = UsageLedger.task_key(runtime.task(sid_of(query['task_id'])))
        ledger = UsageLedger(runtime.root)
        ledger.refresh()
        return ledger, first, last, filters, load_pricing(runtime.root)

    @router.get('/usage')
    def usage(request: Request):
        ledger, first, last, filters, pricing = usage_query(request)
        data = ledger.summary(start_hour=first, end_hour=last, filters=filters, pricing=pricing,
            granularity=request.query_params.get('granularity', 'day'), group_by=request.query_params.get('group_by', ''))
        data.update(timezone=os.environ.get('TZ') or datetime.now().astimezone().tzname(), ledger=ledger.info())
        return data

    @router.get('/usage/tasks')
    def usage_tasks(request: Request):
        ledger, first, last, filters, pricing = usage_query(request)
        return ledger.top_tasks(start_hour=first, end_hour=last, filters=filters, pricing=pricing,
                                limit=min(500, max(1, int(request.query_params.get('limit', '20')))))

    @router.post('/usage/rebuild')
    def rebuild_usage():
        from utils.usage_ledger import UsageLedger
        ledger = UsageLedger(runtime.root)
        ledger.refresh(rebuild=True)
        return {'status': 'success', 'ledger': ledger.info()}

    @synchronized
    def start(body):
        record = ensure(body)
        from infiagent import InfiAgent
        allowed = set(inspect.signature(InfiAgent.run).parameters) - {'self', 'task_id', 'user_input',
                  'agent_system', 'agent_name', 'workspace_id', 'on_event', 'collect_events'}
        raw = body.get('sdk_overrides') or {}
        if not isinstance(raw, dict):
            raise ValueError('sdk_overrides must be an object')
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError('Unsupported SDK override(s): ' + ', '.join(unknown))
        overrides = {k: v for k, v in raw.items() if k in allowed}
        overrides.update({k: v for k, v in body.items() if k in allowed})
        message = Message(session_id=record['id'], message=body.get('new_message') or body.get('message') or 'Continue',
                          agent_system=record['agent_system'], agent_name=record['agent_name'])
        return runtime.start(message, sdk_overrides=overrides)

    def map_event(value, max_output_chars=32000):
        common = {'seq': value['id'], 'run_id': value.get('run_id', ''), 'timestamp': value.get('timestamp', '')}
        kind = value.get('type')
        if kind == 'message':
            return {**common, 'type': 'run.started'}
        if kind == 'result':
            result = value['result']
            return {**common, 'type': 'message.done', 'result': result, 'text': result.get('output', ''),
                    'usage': result.get('usage', {})}
        if kind == 'finished':
            return {**common, 'type': 'run.finished', 'status': value['status']}
        if kind == 'interaction':
            return {**common, **value['interaction'], 'type': 'human_in_loop'}
        if kind != 'event':
            return None
        event = value['event']
        payload = event.get('payload', {})
        name = event.get('event_type')
        types = {'agent.start': 'agent.started', 'agent.end': 'agent.finished', 'run.tool.start': 'tool.started',
                 'run.tool.end': 'tool.finished', 'run.thinking.start': 'thinking.started',
                 'run.thinking.end': 'thinking.done', 'run.thinking.token': 'thinking.delta',
                 'system.error': 'run.error'}
        mapped = types.get(name)
        text = payload.get('text') or payload.get('result', '')
        if name == 'run.llm.token':
            mapped = 'assistant.reasoning.delta' if payload.get('token_kind') == 'reasoning' else 'assistant.content.delta'
        if name == 'run.llm.end':
            mapped = 'assistant.content.done'
            text = payload.get('output') or ''
        if not mapped:
            return None
        result = {**common, **payload, 'type': mapped}
        timestamp = result.get('timestamp')
        if isinstance(timestamp, (int, float)):
            result['timestamp'] = datetime.fromtimestamp(timestamp).isoformat(timespec='seconds')
        if mapped == 'tool.finished' and isinstance(result.get('result'), dict):
            tool_result = dict(result['result'])
            output = tool_result.get('output')
            if isinstance(output, str) and len(output) > max_output_chars:
                tool_result.update(output=output[:max_output_chars], output_preview=output[:max_output_chars],
                                   output_ref={'seq': value['id'], 'run_id': value.get('run_id', ''), 'bytes': len(output.encode('utf-8'))})
            result['result'] = tool_result
        if mapped in {'thinking.done', 'thinking.delta', 'assistant.content.delta', 'assistant.content.done', 'assistant.reasoning.delta'}:
            result['text'] = text if isinstance(text, str) else ''
        if mapped == 'run.error':
            result['error'] = payload.get('error_display', '')
        return result

    async def stream(sid, after=0):
        path = runtime.events_file(sid)
        offset = 0
        shown_interactions = set()
        while True:
            if path.exists():
                with path.open(encoding='utf-8') as source:
                    source.seek(offset)
                    while True:
                        start_pos = source.tell()
                        line = source.readline()
                        if not line or not line.endswith('\n'):
                            offset = start_pos
                            break
                        offset = source.tell()
                        value = json.loads(line)
                        if value['id'] <= after:
                            continue
                        event = map_event(value)
                        if event:
                            yield f'id: {value["id"]}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
            for _, record in interactions.records(runtime.task(sid)):
                occurrence = record['occurrence_id']
                if record['status'] == 'waiting' and occurrence not in shown_interactions:
                    shown_interactions.add(occurrence)
                    event = interaction_event(record)
                    yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
            if runtime.session(sid)['status'] not in {'running', 'stopping'}:
                break
            yield ': keepalive\n\n'
            await asyncio.sleep(.2)

    @router.post('/tasks/message/stream')
    def send_stream(body: dict):
        record = ensure(body)
        sid = record['id']
        # Allocate the event cursor before starting the next run.
        path = runtime.events_file(sid)
        after = 0
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                after = max(after, int(json.loads(line)['id']))
        start(body)
        return StreamingResponse(stream(sid, after), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    @router.get('/tasks/live-stream')
    def live(task_id: str, after_seq: int = 0, after_id: str = '', run_id: str = ''):
        sid = sid_of(task_id)
        after = max(after_seq, int(after_id.split('.')[0])) if after_id else after_seq
        return StreamingResponse(stream(sid, after), media_type='text/event-stream')

    @router.post('/tasks/message')
    async def send_sync(body: dict):
        record = start(body)
        sid = record['id']
        while runtime.session(sid)['status'] in {'running', 'stopping'}:
            await asyncio.sleep(.2)
        done = runtime.session(sid)
        return {'status': 'success', 'task_id': sid, 'run_id': done.get('run_id'),
                'result': done.get('result'), 'usage': (done.get('result') or {}).get('usage', {})}

    @router.post('/tasks/add-message')
    @synchronized
    def add(body: dict):
        record = ensure(body)
        if record['id'] not in runtime.processes and not body.get('resume_if_needed', False):
            message = str(body.get('new_message') or '').strip()
            if not message:
                raise ValueError('Message is empty')
            with runtime.scope():
                from core.hierarchy_manager import get_hierarchy_manager
                get_hierarchy_manager(str(runtime.task(record['id']))).append_instruction(message, source='user')
            write_json(runtime.sessions / (record['id'] + '.json'), {**record,
                       'queued_messages': [*(record.get('queued_messages') or []), message]})
            runtime.append(record['id'], {'type': 'message', 'role': 'user', 'content': message})
            return {'status': 'success', 'task_id': record['id'], 'queued': True}
        result = start(body)
        return {'status': 'success', 'task_id': result['id'], 'run_id': result.get('run_id')}

    @router.post('/tasks/pause')
    def pause(body: dict):
        return runtime.stop(sid_of(body['task_id']))

    @router.post('/tasks/resume')
    def resume(body: dict):
        record = runtime.session(sid_of(body['task_id']))
        queued = '\n\n'.join(record.get('queued_messages') or [])
        result = start({**body, 'new_message': body.get('new_message') or queued or 'Continue the interrupted task.'})
        return {'status': 'success', 'task_id': result['id'], 'run_id': result.get('run_id')}

    @router.post('/tasks/fresh')
    def fresh(body: dict):
        sid = sid_of(body['task_id'])
        with runtime.scope():
            from utils.runtime_control import request_fresh
            request_fresh('developer console', str(runtime.task(sid)))
        return {'status': 'success', 'task_id': sid}

    @router.post('/tasks/reset')
    @synchronized
    def reset(body: dict):
        sid = sid_of(body['task_id'])
        if sid in runtime.processes:
            raise HTTPException(409, 'Stop the running task before resetting it')
        from infiagent import infiagent
        agent = infiagent(user_data_root=str(runtime.root), seed_builtin_resources=False)
        result = agent.reset_task(task_id=str(runtime.task(sid)), preserve_history=bool(body.get('preserve_history', True)),
                                  kill_background_processes=False, reason='developer console reset')
        if result['status'] == 'success':
            write_json(runtime.sessions / (sid + '.json'), {**runtime.session(sid), 'status': 'ready'})
        return result

    @router.delete('/tasks')
    @synchronized
    def delete_task(task_id: str):
        sid = sid_of(task_id)
        task = runtime.task(sid)
        result = reset({'task_id': sid, 'preserve_history': False})
        if result.get('status') != 'success':
            return result
        trash = runtime.root / '.trash' / (sid + '-' + uuid.uuid4().hex[:8])
        trash.parent.mkdir(exist_ok=True)
        task.rename(trash)
        (runtime.sessions / (sid + '.json')).unlink()
        return {'status': 'success', 'task_id': sid}

    @router.get('/tasks/snapshot')
    def snapshot(task_id: str):
        sid = sid_of(task_id)
        from infiagent import infiagent
        agent = infiagent(user_data_root=str(runtime.root), seed_builtin_resources=False)
        return agent.task_snapshot(task_id=str(runtime.task(sid)))

    @router.get('/tasks/history')
    def history(task_id: str, max_output_chars: int = 6000):
        sid = sid_of(task_id)
        path = runtime.events_file(sid)
        items, process = [], []
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                value = json.loads(line)
                if value.get('type') == 'message':
                    items.append({'type': 'user_message', 'text': value['content']})
                elif value.get('type') == 'result':
                    result = value['result']
                    items.append({'type': 'agent_turn', 'text': result.get('output') or result.get('error_information', ''),
                                  'process': process, 'run_id': value.get('run_id'), 'usage': result.get('usage', {})})
                    process = []
                elif value.get('type') == 'event':
                    event = map_event(value, max(128, min(64000, max_output_chars)))
                    if event and event['type'] in {'tool.started', 'tool.finished'}:
                        process.append({**event, 'type': 'tool', 'name': event.get('tool_name'),
                                        'status': 'running' if event['type'] == 'tool.started' else event.get('status', 'done')})
                    elif event and event['type'] == 'thinking.done':
                        process.append({**event, 'type': 'thinking'})
        record = runtime.session(sid)
        return {'items': items, 'task_id': sid, 'live_run_id': record.get('run_id') if record['status'] in {'running', 'stopping'} else None}

    @router.get('/tasks/tool-output')
    def tool_output(task_id: str, seq: int, run_id: str = ''):
        path = runtime.events_file(sid_of(task_id))
        if path.exists():
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    record = json.loads(line)
                    if record.get('id') == seq and (not run_id or record.get('run_id') == run_id):
                        event = record.get('event') or {}
                        if event.get('event_type') != 'run.tool.end':
                            break
                        return {'output': (event.get('payload', {}).get('result') or {}).get('output', '')}
        raise HTTPException(404, 'Tool output not found')

    @router.get('/tasks/files')
    def files(task_id: str, path: str = '', area: str = 'business'):
        sid = sid_of(task_id)
        directory, root = file_path(sid, path, area)
        if not directory.is_dir():
            raise HTTPException(404, 'Directory not found')
        return {'entries': [{'name': p.name, 'path': str(p.relative_to(root)), 'type': 'dir' if p.is_dir() else 'file',
                             'size': p.stat().st_size if p.is_file() else 0}
                            for p in sorted(directory.iterdir()) if not p.is_symlink() and (area == 'session' or p.name != '.sdk_runtime')]}

    @router.get('/tasks/artifact')
    def artifact(task_id: str, path: str, area: str = 'business'):
        file, _ = file_path(sid_of(task_id), path, area)
        if not file.is_file():
            raise HTTPException(404, 'File not found')
        return FileResponse(file, filename=file.name)

    @router.delete('/tasks/artifact')
    def delete_file(task_id: str, path: str):
        sid = sid_of(task_id)
        file, _ = file_path(sid, path)
        if not file.is_file():
            raise HTTPException(404, 'File not found')
        with runtime.scope(), workspaces.file_access(runtime.task(sid), blocking=False):
            file.unlink()
        return {'status': 'success'}

    @router.post('/tasks/upload-files')
    async def upload(request: Request):
        form = await request.form()
        sid = sid_of(form.get('task_id'))
        destination = str(form.get('dest_subdir') or '.upload')
        uploaded = []
        for item in form.getlist('files'):
            name = Path(item.filename).name
            if not name or name in {'.', '..'}:
                raise ValueError('Invalid upload filename')
            file, _ = file_path(sid, str(Path(destination) / name))
            content = await item.read(20 * 1024 * 1024 + 1)
            if len(content) > 20 * 1024 * 1024:
                raise HTTPException(413, 'File limit is 20 MiB')
            with runtime.lock, runtime.scope(), workspaces.file_access(runtime.task(sid), blocking=False):
                file, _ = file_path(sid, str(Path(destination) / name))
                file.parent.mkdir(parents=True, exist_ok=True)
                temporary = file.with_name('.upload-' + uuid.uuid4().hex)
                try:
                    temporary.write_bytes(content)
                    os.replace(temporary, file)
                finally:
                    temporary.unlink(missing_ok=True)
            uploaded.append({'name': name, 'path': str(Path(destination) / name)})
        return {'status': 'success', 'items': uploaded}

    @router.get('/tasks/uploads')
    def uploads(task_id: str):
        try:
            return files(task_id, '.upload')
        except HTTPException:
            return {'entries': []}

    @router.get('/tasks/pending-hil')
    def pending(task_id: str):
        return {'items': [interaction_event(r) for _, r in interactions.records(runtime.task(sid_of(task_id))) if r['status'] == 'waiting']}

    def interaction_event(record):
        if record.get('kind') == 'tool_confirmation':
            return {'type': 'tool_confirmation', 'confirm_id': record['occurrence_id'],
                    'tool_name': record.get('tool_name'), 'arguments': record.get('arguments', {})}
        return {'type': 'human_in_loop', 'hil_id': record['hil_id'], 'occurrence_id': record['occurrence_id'],
                'message': record['instruction'], 'title': 'Human-in-Loop'}

    @router.post('/tasks/hil-respond')
    def answer(body: dict):
        return interactions.answer(body.get('occurrence_id') or body['hil_id'], body.get('response', ''),
                                   runtime.task(sid_of(body['task_id'])))

    @router.post('/tasks/tool-confirm')
    def confirm_tool(body: dict):
        return interactions.answer(body['confirm_id'], 'approved' if body.get('approved') else 'rejected',
                                   runtime.task(sid_of(body['task_id'])))

    @router.get('/human-confirmations')
    def confirmations():
        return {'requests': []}

    @router.get('/tasks/feedback')
    def feedback(task_id: str):
        path = runtime.task(sid_of(task_id)) / '.sdk_runtime' / 'feedback.json'
        return read_json(path, missing={'latest_by_run': {}})

    @router.post('/tasks/feedback')
    async def write_feedback(request: Request):
        form = await request.form()
        sid = sid_of(form.get('task_id'))
        path = runtime.task(sid) / '.sdk_runtime' / 'feedback.json'
        record = read_json(path, missing={'latest_by_run': {}})
        value = {k: v for k, v in form.items() if isinstance(v, str)}
        record['latest_by_run'][value.get('run_id', '')] = value
        write_json(path, record)
        return {'status': 'success'}

    @router.post('/tasks/adopt')
    def adopt(body: dict):
        sid = sid_of(body['task_id'])
        path = runtime.task(sid) / '.sdk_runtime' / 'adoptions.json'
        values = read_json(path, missing={})
        values[str(body.get('run_id') or '')] = {'version_id': body.get('version_id'), 'at': time.time()}
        write_json(path, values)
        return {'status': 'success'}

    @router.post('/artifacts/markdown-tables.xlsx')
    def tables(body: dict):
        from openpyxl import Workbook
        text = str(body.get('markdown') or '')
        if len(text) > 1000000:
            raise ValueError('Markdown is too large')
        lines = text.splitlines()
        book = Workbook()
        book.remove(book.active)
        index = 0
        while index + 1 < len(lines):
            if '|' not in lines[index] or not re.fullmatch(r'[\s|:\-]+', lines[index + 1]) or '-' not in lines[index + 1]:
                index += 1
                continue
            sheet = book.create_sheet(f'Table {len(book.sheetnames) + 1}')
            header = lines[index]
            rows = [header]
            index += 2
            while index < len(lines) and '|' in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            for row in rows:
                values = [cell.strip() for cell in row.strip().strip('|').split('|')]
                sheet.append(["'" + value if value.startswith(('=', '+', '-', '@')) else value for value in values])
        if not book.sheetnames:
            raise ValueError('No Markdown tables found')
        output = io.BytesIO()
        book.save(output)
        return Response(output.getvalue(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': 'attachment; filename="tables.xlsx"'})

    @router.post('/runtime/upload-user-root')
    async def upload_root(request: Request):
        if runtime.processes:
            raise HTTPException(409, 'Stop all runs before importing configuration')
        form = await request.form(max_files=10000)
        mode = str(form.get('mode') or 'merge_overwrite')
        if mode not in {'merge_overwrite', 'merge_skip'}:
            raise ValueError('Use merge_overwrite or merge_skip')
        contents = []
        archive = form.get('archive')
        if archive:
            raw = await archive.read(100 * 1024 * 1024 + 1)
            if len(raw) > 100 * 1024 * 1024:
                raise HTTPException(413, 'Archive is too large')
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                if len(package.infolist()) > 10000 or sum(i.file_size for i in package.infolist()) > 200 * 1024 * 1024:
                    raise HTTPException(413, 'Extracted archive is too large')
                for item in package.infolist():
                    if stat.S_ISLNK(item.external_attr >> 16):
                        raise ValueError('Archive symlinks are not accepted')
                    if not item.is_dir():
                        contents.append((item.filename, package.read(item)))
        else:
            total = 0
            for item in form.getlist('files'):
                raw = await item.read(20 * 1024 * 1024 + 1)
                total += len(raw)
                if len(raw) > 20 * 1024 * 1024 or total > 200 * 1024 * 1024:
                    raise HTTPException(413, 'Upload is too large')
                contents.append((item.filename, raw))
        allowed = {'config', 'agent_library', 'tools_library', 'skills', 'resources', 'knowledge', 'system-add'}
        planned = []
        for name, raw in contents:
            path = Path(name)
            parts = path.parts
            if path.is_absolute() or '..' in parts or '\\' in name:
                raise ValueError('Invalid archive path')
            if str(form.get('strip_top_level', 'true')).lower() == 'true' and parts and parts[0] not in allowed:
                parts = parts[1:]
            if not parts or parts[0] not in allowed:
                continue
            target = (runtime.root / Path(*parts)).resolve()
            if not target.is_relative_to(runtime.root):
                raise ValueError('Upload path escapes user_root')
            planned.append((target, raw))
        if not planned:
            raise ValueError('No configuration or resource files found')
        from utils.configuration_lock import configuration_access
        with runtime.lock, configuration_access(runtime.root, write=True):
            if runtime.processes:
                raise HTTPException(409, 'Stop all runs before importing configuration')
            count = 0
            for target, raw in planned:
                if mode == 'merge_skip' and target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name('.upload-' + uuid.uuid4().hex)
                try:
                    temporary.write_bytes(raw)
                    if target.is_relative_to(runtime.root / 'config'):
                        temporary.chmod(0o600)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                count += 1
        return {'status': 'success', 'written_files': count, 'active_user_root': str(runtime.root)}

    app.include_router(router)
