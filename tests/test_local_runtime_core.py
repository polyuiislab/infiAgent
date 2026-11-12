import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from utils.atomic_state import write_json, read_json, StateCommitError, StateCorruptError
from utils.agent_activities import AgentActivities
from utils.execution_scope import ExecutionScope, CURRENT, bind, check_cancelled, tool_call
from utils import interactions, workspaces


def test_failed_replace_preserves_previous_record(tmp_path):
    path = tmp_path / 'record.json'
    write_json(path, {'before': True})
    with patch('utils.atomic_state.os.replace', side_effect=OSError('disk failure')):
        with pytest.raises(StateCommitError):
            write_json(path, {'after': True})
    assert read_json(path) == {'before': True}
    assert list(tmp_path.iterdir()) == [path]


def test_corruption_is_not_missing_state(tmp_path):
    path = tmp_path / 'record.json'
    path.write_text('{')
    with pytest.raises(StateCorruptError):
        read_json(path)
    assert read_json(tmp_path / 'missing') is None


def test_parallel_same_named_children_have_distinct_identity_and_receipts(tmp_path):
    barrier = threading.Barrier(2)
    identities = []
    def child(name, value, mode):
        identities.append(CURRENT.get().instance_key)
        barrier.wait(timeout=2)
        return {'status': 'success', 'output': value}
    manager = AgentActivities(tmp_path, 'parent', child)
    try:
        a = manager.submit('worker', 'one', 'call', 0)
        b = manager.submit('worker', 'two', 'call', 1)
        result = manager.collect([a, b], 3)
        assert [x['result']['output'] for x in result['results']] == ['one', 'two']
        assert len(set(identities)) == 2
        assert manager.outstanding() == [a, b]
        manager.acknowledge([a, b])
        assert manager.outstanding() == []
    finally:
        manager.close()


def test_completed_result_survives_restart_without_reexecution(tmp_path):
    calls = []
    def child(*args):
        calls.append(args)
        return {'status': 'success', 'output': 'saved'}
    first = AgentActivities(tmp_path, 'parent', child)
    handle = first.submit('worker', 'input', 'call')
    assert len(first.collect([handle], 2)['results']) == 1
    first.close()
    second = AgentActivities(tmp_path, 'parent', child)
    try:
        assert second.submit('worker', 'input', 'call') == handle
        assert second.collect([handle])['results'][0]['result']['output'] == 'saved'
        assert len(calls) == 1
    finally:
        second.close()


def test_collect_any_and_cancel_running_child(tmp_path):
    entered = threading.Event()
    def child(name, value, mode):
        if value == 'slow':
            entered.set()
            while True:
                check_cancelled()
                threading.Event().wait(.01)
        return {'status': 'success', 'output': value}
    manager = AgentActivities(tmp_path, 'parent', child)
    try:
        slow = manager.submit('worker', 'slow', 's')
        fast = manager.submit('worker', 'fast', 'f')
        assert entered.wait(1)
        first = manager.collect([slow, fast], 2, k=1)
        assert [x['handle'] for x in first['results']] == [fast]
        manager.cancel([slow])
        assert manager.collect([slow], 2)['results'][0]['result']['status'] == 'cancelled'
    finally:
        manager.close()


def test_spawn_does_not_start_if_intent_cannot_commit(tmp_path):
    calls = []
    manager = AgentActivities(tmp_path, 'parent', lambda *args: calls.append(args))
    try:
        with patch('utils.agent_activities.write_json', side_effect=StateCommitError('disk')):
            with pytest.raises(StateCommitError):
                manager.submit('worker', 'input', 'call')
        assert calls == []
        assert manager.futures == {}
    finally:
        manager.close()


def test_nested_spawn_has_depth_limit(tmp_path):
    manager = AgentActivities(tmp_path, 'child', lambda *args: None, scope=ExecutionScope(depth=1))
    try:
        with pytest.raises(ValueError, match='depth'):
            manager.submit('worker', 'input', 'call')
    finally:
        manager.close()


def test_tool_scope_prevents_execution(tmp_path):
    from core.tool_executor import ToolExecutor
    config = SimpleNamespace(agent_system_name='test', get_tool_config=lambda name: {'type': 'tool_call_agent'})
    executor = ToolExecutor(config, SimpleNamespace(get_context=lambda: {}))
    executor.available_agent_tools = {'allowed'}
    with patch.object(executor, '_call_direct') as call:
        result = executor.execute('other', {}, str(tmp_path))
    assert result['error_code'] == 'SCOPE_DENIED'
    call.assert_not_called()


def test_hil_alias_collision_is_reported_and_occurrences_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('MLA_USER_DATA_ROOT', str(tmp_path))
    with tool_call('c1'):
        pa, a = interactions.create(tmp_path / 'tasks/a', 'approve', 'first')
    with tool_call('c2'):
        pb, b = interactions.create(tmp_path / 'tasks/b', 'approve', 'second')
    with pytest.raises(ValueError, match='Ambiguous'):
        interactions.answer('approve', 'wrong')
    assert interactions.answer(a['occurrence_id'], 'yes')['success']
    assert read_json(pa)['result'] == 'yes'
    assert read_json(pb)['status'] == 'waiting'
    interactions._paths.clear()  # Simulated interpreter restart; reload from the task.
    assert interactions.find(b['occurrence_id'], tmp_path / 'tasks/b')[1]['instruction'] == 'second'


def test_workspace_files_shared_but_task_records_separate(tmp_path, monkeypatch):
    monkeypatch.setenv('MLA_USER_DATA_ROOT', str(tmp_path))
    workspaces.create('demo')
    a, b = tmp_path / 'tasks/a', tmp_path / 'tasks/b'
    workspaces.attach(a, 'demo')
    workspaces.attach(b, 'demo')
    workspaces.resolve_file(a, 'shared.txt').write_text('shared')
    assert workspaces.resolve_file(b, 'shared.txt').read_text() == 'shared'
    write_json(a / '.sdk_runtime/private.json', {'task': 'a'})
    assert not (b / '.sdk_runtime/private.json').exists()
    with pytest.raises(ValueError):
        workspaces.resolve_file(a, '../workspace.json')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (workspaces.files_root(a) / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        workspaces.resolve_file(a, 'link/secret')
