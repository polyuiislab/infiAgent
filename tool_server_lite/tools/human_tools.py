"""Human interaction tools backed by task-local durable records."""
from pathlib import Path
from typing import Dict, Any
import asyncio
from .file_tools import BaseTool
from utils.event_emitter import get_event_emitter
from utils import interactions
from utils.atomic_state import read_json, write_json
from utils.execution_scope import check_cancelled, AgentCancelled

# Compatibility observation cache; durable records are authoritative.
HIL_TASKS = {}
TOOL_CONFIRMATIONS = {}

class HumanInLoopTool(BaseTool):
    async def execute_async(self, task_id, parameters):
        hil_id = parameters.get('hil_id')
        instruction = parameters.get('instruction')
        if not isinstance(hil_id, str) or not hil_id or not isinstance(instruction, str) or not instruction:
            return {'status': 'error', 'output': '', 'error': 'hil_id and instruction are required'}
        timeout = parameters.get('timeout')
        if timeout is not None and float(timeout) < 0:
            return {'status': 'error', 'output': '', 'error': 'timeout must be nonnegative'}
        path, record = interactions.create(task_id, hil_id, instruction)
        occurrence = record['occurrence_id']
        HIL_TASKS[occurrence] = record
        get_event_emitter().human_in_loop(hil_id=occurrence, title='Human-in-Loop',
            message=instruction, ui={'type': 'text'},
            timeout_sec=int(timeout) if timeout is not None else 1800,
            resume_hint='Reply using this occurrence ID')
        began = asyncio.get_running_loop().time()
        try:
            while True:
                check_cancelled()
                record = read_json(path)
                if record['status'] == 'completed':
                    return {'status': 'success', 'output': str(record['result']), 'error': ''}
                if record['status'] in {'cancelled', 'timeout'}:
                    return {'status': 'error', 'output': '', 'error': record['status']}
                if timeout is not None and asyncio.get_running_loop().time() - began >= float(timeout):
                    interactions.end_wait(path, 'timeout')
                    return {'status': 'error', 'output': '', 'error': 'Human task timeout'}
                await asyncio.sleep(.1)
        except AgentCancelled:
            interactions.end_wait(path, 'cancelled')
            raise
        finally:
            HIL_TASKS.pop(occurrence, None)

def get_hil_status(hil_id, task_id=None):
    try:
        _, record = interactions.find(hil_id, task_id)
        return {'found': True, **record} if record else {'found': False, 'error': 'HIL task not found'}
    except ValueError as exc:
        return {'found': False, 'error': str(exc)}

def respond_hil_task(hil_id, response, task_id=None):
    try:
        return interactions.answer(hil_id, response, task_id)
    except ValueError as exc:
        return {'success': False, 'error': str(exc)}

def list_hil_tasks(task_id=None):
    tasks = [record for _, record in interactions.records(task_id) if record['status'] == 'waiting' and record.get('kind') != 'tool_confirmation']
    return {'total': len(tasks), 'tasks': tasks}

def get_hil_task_for_workspace(task_id):
    tasks = list_hil_tasks(task_id)['tasks']
    return {'found': True, **tasks[0]} if tasks else {'found': False}

# Tool approval uses the same durable occurrence store.
def create_tool_confirmation(confirm_id: str, task_id: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    path, record = interactions.create(task_id, confirm_id, 'Approve tool: ' + tool_name)
    record = {**record, 'kind': 'tool_confirmation', 'tool_name': tool_name, 'arguments': arguments}
    write_json(path, record)
    TOOL_CONFIRMATIONS[confirm_id] = record
    return {'success': True, 'confirm_id': record['occurrence_id']}


def get_tool_confirmation_status(confirm_id: str) -> Dict[str, Any]:
    try:
        _, record = interactions.find(confirm_id)
    except ValueError as exc:
        return {'found': False, 'error': str(exc)}
    if not record:
        return {'found': False, 'error': 'Tool confirmation not found'}
    return {'found': True, **record, 'confirm_id': record['occurrence_id']}


def respond_tool_confirmation(confirm_id: str, approved: bool) -> Dict[str, Any]:
    return interactions.answer(confirm_id, 'approved' if approved else 'rejected')


def get_tool_confirmation_for_workspace(task_id: str) -> Dict[str, Any]:
    records = [r for _, r in interactions.records(task_id) if r.get('kind') == 'tool_confirmation' and r['status'] == 'waiting']
    return {'found': True, **records[0], 'confirm_id': records[0]['occurrence_id']} if records else {'found': False}


def list_tool_confirmations() -> Dict[str, Any]:
    records = [{**r, 'confirm_id': r['occurrence_id']} for _, r in interactions.records()
               if r.get('kind') == 'tool_confirmation' and r['status'] == 'waiting']
    return {'total': len(records), 'confirmations': records}
