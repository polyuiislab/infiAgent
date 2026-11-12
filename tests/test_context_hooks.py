#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import tempfile
import unittest
from pathlib import Path

from core.context_builder import ContextBuilder
from utils.user_paths import runtime_env_scope


class _DummyHierarchyManager:
    def __init__(self):
        self._context = {"current": {"instructions": [], "hierarchy": {}, "agents_status": {}}, "history": []}

    def get_context(self):
        return self._context

    def get_loaded_skills(self, agent_id):
        return []

    def _save_context(self, context):
        self._context = context


class _DummyLoader:
    agent_config_dir = "."


class ContextHookTests(unittest.TestCase):
    def test_after_build_hook_can_modify_context_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            hook_file = root / "hook.py"
            hook_file.write_text(
                "def on_context(payload):\n"
                "    return {'context_text': payload['context_text'] + '\\n\\n<test-hook>enabled</test-hook>'}\n",
                encoding="utf-8",
            )
            with runtime_env_scope({
                "MLA_USER_DATA_ROOT": str(root),
                "MLA_CONTEXT_HOOKS_JSON": json.dumps([{"name": "demo", "when": "after_build", "callback": f"{hook_file}:on_context"}]),
            }):
                builder = ContextBuilder(
                    _DummyHierarchyManager(),
                    agent_config={"prompts": {}},
                    config_loader=_DummyLoader(),
                    llm_client=None,
                )
                context = builder.build_context(
                    task_id=str(root / "task"),
                    agent_id="agent_demo",
                    agent_name="alpha_agent",
                    task_input="hello",
                    action_history=[],
                    include_action_history=False,
                )
            self.assertIn("<test-hook>enabled</test-hook>", context)


if __name__ == "__main__":
    unittest.main()
