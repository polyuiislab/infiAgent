"""Shared project files are portable; no developer-machine paths required."""
from utils import workspaces
from utils.atomic_state import write_json
from tool_server_lite.tools.file_tools import get_abs_path


def test_two_sessions_resolve_the_same_shared_file(tmp_path, monkeypatch):
    monkeypatch.setenv('MLA_USER_DATA_ROOT', str(tmp_path))
    workspaces.create('shared-catalog')
    a, b = tmp_path / 'tasks/a', tmp_path / 'tasks/b'
    workspaces.attach(a, 'shared-catalog')
    workspaces.attach(b, 'shared-catalog')
    file = get_abs_path(str(a), 'catalog.json')
    write_json(file, {'items': ['alpha-profile', 'beta-profile']})
    assert get_abs_path(str(b), 'catalog.json') == file
    assert 'alpha-profile' in get_abs_path(str(b), 'catalog.json').read_text()
