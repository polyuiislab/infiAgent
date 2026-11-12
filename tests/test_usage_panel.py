from datetime import datetime
from fastapi.testclient import TestClient
from infiagent.runtime.server import create_app
from utils.usage_ledger import UsageLedger
from utils.usage_metrics import parse_range, load_pricing


def test_usage_deduplication_cache_price_and_rebuild(tmp_path):
    ledger = UsageLedger(tmp_path)
    task = tmp_path / 'tasks' / 'sample'
    first = {'event_id': 'one', 'timestamp': datetime(2026, 11, 12, 10, 20).timestamp(),
             'task_id': str(task), 'agent_system': 'demo', 'agent_name': 'worker', 'model': 'openai/mock',
             'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': 40}}}
    second = {**first, 'event_id': 'two', 'usage': {'prompt_tokens': 50, 'completion_tokens': 10}}
    ledger.record(first)
    ledger.record(first)
    ledger.record(second)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/usage_pricing.yaml').write_text('currency: USD\nmodels:\n  mock: {input: 4, cached_input: 1, output: 8}\n')
    start, end = parse_range('2026-11-12T10', '2026-11-12T10')
    def summary():
        return ledger.summary(start_hour=start, end_hour=end, granularity='hour', group_by='model', pricing=load_pricing(tmp_path))
    before = summary()
    assert before['totals']['calls'] == 2
    assert before['totals']['input_tokens'] == 150
    assert before['totals']['cached_input_tokens'] == 40
    assert before['totals']['output_tokens'] == 30
    assert before['totals']['total_tokens'] == 180
    assert before['totals']['cost'] == .00072
    assert len(before['buckets']) == 1
    ledger.refresh()
    assert summary() == before
    ledger.refresh(rebuild=True)
    assert summary() == before
    assert ledger.top_tasks(start_hour=start, end_hour=end)['items'][0]['total_tokens'] == 180


def test_usage_panel_routes_and_empty_range(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/usage.html').status_code == 200
        for granularity in ['hour', 'day', 'week', 'month']:
            response = client.get('/api/v1/usage', params={'granularity': granularity, 'from': '2026-11-12', 'to': '2026-11-12'})
            assert response.status_code == 200, response.text
            assert response.json()['totals']['calls'] == 0
            assert response.json()['buckets']
        assert client.get('/api/v1/usage?from=bad').status_code == 400
        assert client.get('/api/v1/usage/tasks').json()['count'] == 0
