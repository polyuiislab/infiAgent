"""Real SDK/executor/storage pipeline with a deterministic model boundary."""
import threading
from pathlib import Path
from unittest.mock import patch

import yaml
from infiagent import infiagent
from services.llm_client import LLMResponse, ToolCall
from utils.execution_scope import CURRENT, ExecutionScope, bind


def test_sdk_parallel_children_finish_and_reuse_completed_parent(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    config = root / 'config'
    config.mkdir(parents=True)
    (config / 'llm_config.yaml').write_text(yaml.safe_dump({
        'api_key': 'local-test-only', 'default_model': 'openai/mock', 'models': ['openai/mock']}))
    system = root / 'agent_library' / 'parallel_demo'
    system.mkdir(parents=True)
    (system / 'general_prompts.yaml').write_text('general_prompts: {}\n')
    (system / 'level_0_tools.yaml').write_text(yaml.safe_dump({'tools': {
        'final_output': {'type': 'tool_call_agent', 'level': 0,
                         'parameters': {'type': 'object', 'properties': {'output': {'type': 'string'}}}}
    }}))
    schema = {'type': 'object', 'properties': {'task_input': {'type': 'string'}}}
    (system / 'level_3_agents.yaml').write_text(yaml.safe_dump({'tools': {
        'alpha_agent': {'type': 'llm_call_agent', 'level': 3, 'available_tools': ['worker', 'final_output'],
                        'reasoning_mode': 'react_lite', 'parameters': schema},
        'worker': {'type': 'llm_call_agent', 'level': 3, 'available_tools': ['final_output'],
                   'reasoning_mode': 'react_lite', 'parameters': schema}
    }}))
    monkeypatch.setenv('MLA_SEED_BUILTIN_RESOURCES', '0')
    monkeypatch.setenv('MLA_SKILLS_LIBRARY_DIR', str(root / 'skills'))
    monkeypatch.setenv('MLA_USER_DATA_ROOT', str(root))
    monkeypatch.setenv('MLA_PARALLEL_TOOLS_MAX', '2')
    barrier = threading.Barrier(2)
    parent_calls = []
    child_ids = []

    def model(*args, **kwargs):
        scope = CURRENT.get()
        if scope.agent_name == 'worker':
            child_ids.append(scope.agent_id)
            barrier.wait(timeout=3)
            calls = [ToolCall(id='child-final', name='final_output', arguments={'output': 'child done'})]
        elif not parent_calls:
            parent_calls.append(1)
            calls = [ToolCall(id='child-a', name='worker', arguments={'task_input': 'same input'}),
                     ToolCall(id='child-b', name='worker', arguments={'task_input': 'same input'})]
        else:
            parent_calls.append(1)
            calls = [ToolCall(id='parent-final', name='final_output', arguments={'output': 'all done'})]
        return LLMResponse(status='success', output='', tool_calls=calls, model='openai/mock', finish_reason='tool_calls')

    agent = infiagent(user_data_root=str(root), agent_library_dir=str(root),
                      default_agent_system='parallel_demo', seed_builtin_resources=False)
    task = root / 'tasks' / 'demo'
    events = []
    with bind(ExecutionScope()), patch('services.llm_client.SimpleLLMClient._chat_internal', side_effect=model):
        result = agent.run('parallel test', task_id=str(task), on_event=events.append)
        assert result['status'] == 'success', result
        assert result['output'] == 'all done'
        count = len(parent_calls)
        again = agent.run('parallel test', task_id=str(task))
        assert again['output'] == 'all done'
        assert len(parent_calls) == count
    assert len(child_ids) == 2 and len(set(child_ids)) == 2
    starts = [e for e in events if e['event_type'] == 'run.tool.start']
    assert all(e['payload']['agent_id'] for e in starts)
