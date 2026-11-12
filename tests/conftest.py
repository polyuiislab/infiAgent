#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = BACKEND_ROOT.parent

for candidate in (str(BACKEND_ROOT), str(WORKSPACE_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


import pytest

@pytest.fixture(autouse=True)
def isolated_default_user_root(tmp_path, monkeypatch):
    from services.llm_client import reset_shared_deployment_state
    reset_shared_deployment_state()
    monkeypatch.setenv('INFIAGENT_ALLOWED_HOSTS', 'testserver')
    monkeypatch.setenv("MLA_USER_DATA_ROOT", str(tmp_path / "default-user-root"))
    monkeypatch.setenv("MLA_SKILLS_LIBRARY_DIR", str(tmp_path / "default-skills"))
