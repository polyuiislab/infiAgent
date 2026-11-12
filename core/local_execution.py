"""Small local execution layer: durable tool receipts and ordered parallel batches."""
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from utils.atomic_state import read_json, write_json
from utils.execution_scope import check_cancelled, tool_call
from utils.agent_activities import positive_limit


READ_ONLY_TOOLS = frozenset({'file_read', 'dir_list', 'file_search', 'task_history_search'})
CONTROL_TOOLS = frozenset({'final_output', 'human_in_loop', 'load_skill', 'offload_skill',
                          'spawn_subagents', 'collect', 'collect_subagents', 'cancel', 'cancel_subagents'})


class RepeatedToolError(RuntimeError):
    code = 'CONSECUTIVE_TOOL_ERRORS'


class LocalExecution:
    def _cap_tool_output(self, task_id, call_id, result):
        value = result.get('output')
        limit = int(os.environ.get('MLA_TOOL_OUTPUT_MAX_CHARS', '32000'))
        if limit <= 0 or not isinstance(value, str) or len(value) <= limit:
            return result
        from utils.workspaces import resolve_file
        key = hashlib.sha256(f'{self.agent_id}|{call_id}'.encode()).hexdigest()[:32]
        relative = f'.tool_outputs/{key}.txt'
        path = resolve_file(task_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {**result, 'output': value[:limit] + f'\n[Preview truncated. Read full output: {relative}]',
                'output_ref': relative}

    def _check_tool_error_streak(self):
        limit = int(os.environ.get('MLA_MAX_CONSECUTIVE_TOOL_ERRORS', '5'))
        if limit <= 0:
            return
        records = [r for r in self.action_history_fact if not r.get('tool_name', '').startswith('_')][-limit:]
        if len(records) != limit or any(r.get('result', {}).get('status') != 'error' for r in records):
            return
        signatures = {(r.get('tool_name'), r.get('result', {}).get('error_code'),
                       str(r.get('result', {}).get('error_information', ''))[:500]) for r in records}
        if len(signatures) == 1:
            raise RepeatedToolError(f'The same tool error occurred {limit} times; inspect the error before resuming')
    def _tool_config(self, name):
        return getattr(self.config_loader, 'all_tools', {}).get(name, {})

    def _parallel_safe(self, name):
        if name in CONTROL_TOOLS:
            return False
        config = self._tool_config(name)
        if not config:
            return False
        return config.get('parallel_safe', config.get('type') == 'llm_call_agent' or name in READ_ONLY_TOOLS) is True

    def _invoke_tool_durable(self, call, task_id, arguments):
        check_cancelled()
        owner = str(getattr(self, 'agent_id', '') or 'root')
        logical_id = getattr(call, 'logical_id', str(call.id))
        key = hashlib.sha256(f'{owner}|{logical_id}'.encode()).hexdigest()
        path = Path(task_id) / '.sdk_runtime' / 'tool_receipts' / (key + '.json')
        spec = {'name': call.name, 'arguments': arguments}
        previous = read_json(path)
        if previous:
            if previous.get('spec') != spec:
                raise ValueError('Tool call identity reused with different arguments')
            if previous.get('state') == 'completed':
                return copy.deepcopy(previous['result'])
            config = self._tool_config(call.name)
            recoverable = call.name in READ_ONLY_TOOLS | CONTROL_TOOLS or config.get('type') == 'llm_call_agent' or config.get('idempotent') is True
            if not recoverable:
                return {'status': 'error', 'output': '', 'error_code': 'OUTCOME_UNKNOWN',
                        'error_information': 'Interrupted tool may have had side effects. Inspect its outcome before explicitly retrying.'}
        write_json(path, {'spec': spec, 'state': 'started'})
        with tool_call(logical_id):
            result = self.tool_executor.execute(call.name, arguments, task_id)
        write_json(path, {'spec': spec, 'state': 'completed', 'result': result})
        return result

    def _execute_tool_batch(self, calls, task_id, user_input, turn, **metadata):
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("Model returned duplicate tool-call IDs in a single response")
        index = 0
        while index < len(calls):
            check_cancelled()
            stop = index + 1
            if self._parallel_safe(calls[index].name):
                while stop < len(calls) and self._parallel_safe(calls[stop].name):
                    stop += 1
            segment = calls[index:stop]
            if len(segment) == 1:
                result = self._execute_tool_call(segment[0], task_id, user_input, turn, **metadata)
                yield result
                if result is not None:
                    return
            else:
                prepared = []
                for position, call in enumerate(segment):
                    arguments = self._add_uuid_if_needed(call.name, call.arguments)
                    call = SimpleNamespace(id=call.id, name=call.name, arguments=arguments,
                                           logical_id=f'{self.tool_call_counter + position}:{call.id}')
                    prepared.append(call)
                    if not any(item['id'] == call.id for item in self.pending_tools):
                        self.pending_tools.append({'id': call.id, 'name': call.name, 'arguments': arguments,
                                                   'status': 'pending', 'metadata': metadata, 'logical_id': call.logical_id})
                # The entire batch intent is durable before any tool starts.
                self._save_state(task_id, user_input, turn)
                from core.events import ToolCallStartEvent
                for call in prepared:
                    self.event_emitter.dispatch(ToolCallStartEvent(tool_name=call.name,
                        arguments=call.arguments, tool_call_id=call.id))
                with ThreadPoolExecutor(max_workers=positive_limit('MLA_PARALLEL_TOOLS_MAX', 4)) as pool:
                    futures = [pool.submit(contextvars.copy_context().run, self._invoke_tool_durable,
                                           call, task_id, call.arguments) for call in prepared]
                    # Only this thread mutates history, in the original model order.
                    for call, future in zip(prepared, futures):
                        result = self._execute_tool_call(call, task_id, user_input, turn,
                            _prepared=True, _result=future.result(), **metadata)
                        yield result
            index = stop

    def _acknowledge_activity_results(self, result):
        receipts = result.get('_activity_receipts', [])
        manager = getattr(self.tool_executor, 'activities', None)
        if receipts and manager is not None:
            manager.acknowledge(receipts)

    def _auto_collect_children(self, task_id, user_input, turn):
        manager = getattr(self.tool_executor, 'activities', None)
        if manager is None:
            return
        result = manager.collect(timeout_sec=0)
        if not result['results']:
            return
        handles = [item['handle'] for item in result['results']]
        # This is a host notification, not a fabricated model tool call.
        capped = self._cap_tool_output(task_id, 'collect:' + ','.join(handles), {'status': 'success', 'output': json.dumps(result, ensure_ascii=False)})
        record = {'tool_name': '_assistant_text', '_turn': self.llm_turn_counter,
                  'assistant_content': 'Child-agent results:\n' + capped['output'],
                  'arguments': {}, 'result': {**capped, '_activity_receipts': handles}}
        self.action_history.append(record)
        self.action_history_fact.append(copy.deepcopy(record))
        self.llm_turn_counter += 1
        self._save_state(task_id, user_input, turn)
        manager.acknowledge(handles)

    def _recover_pending_tools(self, task_id, turn=0):
        for pending in list(self.pending_tools):
            call = SimpleNamespace(id=pending['id'], name=pending['name'], arguments=pending['arguments'],
                                   logical_id=pending.get('logical_id', f'{self.tool_call_counter}:{pending["id"]}'))
            result = self._execute_tool_call(call, task_id, self.current_task_input, turn,
                                            _prepared=True, **pending.get('metadata', {}))
            if result is not None:
                return result
