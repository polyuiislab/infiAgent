import os

from utils.runtime_control import _is_pid_alive


def test_pid_probe_never_signals_the_process(monkeypatch):
    def forbidden_signal(*args):
        raise AssertionError('a liveness check must not send signals')

    if os.name == 'nt':
        monkeypatch.setattr(os, 'kill', forbidden_signal)
    assert _is_pid_alive(os.getpid())
    assert not _is_pid_alive(0)
    assert not _is_pid_alive(-1)
