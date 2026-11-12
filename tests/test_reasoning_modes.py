#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.agent_executor import AgentExecutor
from services.llm_client import LLMResponse


class _DummyEmitter:
    def __init__(self):
        self.events = []

    def dispatch(self, _event):
        self.events.append(_event)
        return None


class ReasoningModeTests(unittest.TestCase):
    def _bare_executor(self):
        executor = AgentExecutor.__new__(AgentExecutor)
        executor.agent_name = "alpha_agent"
        executor.agent_config = {}
        executor.execution_model = "demo-model"
        executor.available_tools = ["file_write", "task_history_search"]
        executor.config_loader = SimpleNamespace(all_tools={
            "file_write": {
                "description": "写入文件",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件相对路径"},
                        "content": {"type": "string", "description": "写入内容"},
                    },
                    "required": ["path", "content"],
                },
            },
            "task_history_search": {
                "description": "检索历史任务",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "关键词"},
                    },
                    "required": [],
                },
            },
        })
        executor.action_history = []
        executor.action_history_fact = []
        executor.execution_traces = []
        executor.thinking_traces = []
        executor.llm_turn_counter = 0
        executor.event_emitter = _DummyEmitter()
        executor.stream_llm_tokens = False
        return executor

    def test_reasoning_mode_aliases_and_policy_flags(self):
        self.assertEqual(AgentExecutor._normalize_reasoning_mode("react(lite)"), "react_lite")
        self.assertEqual(AgentExecutor._normalize_reasoning_mode("react-lite"), "react_lite")
        self.assertEqual(AgentExecutor._normalize_reasoning_mode(thinking_enabled=False), "react")

        executor = self._bare_executor()

        executor.reasoning_mode = "thinking"
        self.assertFalse(executor._should_run_react_reflection())
        self.assertTrue(executor._tool_call_required_for_response())
        self.assertIsNone(executor._execution_tool_choice_override())

        executor.reasoning_mode = "react"
        self.assertTrue(executor._should_run_react_reflection())
        self.assertFalse(executor._tool_call_required_for_response())
        self.assertIsNone(executor._execution_tool_choice_override())

        executor.reasoning_mode = "react_lite"
        self.assertFalse(executor._should_run_react_reflection())
        self.assertFalse(executor._tool_call_required_for_response())
        self.assertEqual(executor._execution_tool_choice_override(), "auto")

    def test_react_lite_execution_call_allows_auto_tool_choice(self):
        executor = self._bare_executor()
        executor.reasoning_mode = "react_lite"
        captured = {}

        class FakeClient:
            max_context_window = 128000

            def resolve_tool_choice(self, **_kwargs):
                return "auto"

            def chat(self, **kwargs):
                captured.update(kwargs)
                return LLMResponse(
                    status="success",
                    output="",
                    tool_calls=[],
                    model="demo-model",
                    finish_reason="stop",
                    reasoning_content="",
                )

        executor.llm_client = FakeClient()

        executor._execute_llm_call(
            "system",
            [{"role": "user", "content": "next"}],
            tool_choice=executor._execution_tool_choice_override(),
        )

        self.assertEqual(captured["tool_choice"], "auto")
        self.assertEqual(executor.execution_traces[-1]["tool_choice"], "auto")

    def test_agent_thinking_enabled_legacy_config_overrides_runtime_default_mode(self):
        executor = self._bare_executor()
        executor.agent_config = {"thinking_enabled": False}

        executor._apply_runtime_settings({
            "reasoning_mode": "thinking",
            "thinking_enabled": True,
            "action_window_steps": 30,
            "no_tool_retry_limit": 7,
            "fresh_enabled": False,
            "fresh_interval_sec": 0,
        })

        self.assertEqual(executor.reasoning_mode, "react")
        self.assertFalse(executor.thinking_enabled)

    def test_build_messages_includes_react_reflection_and_text_only_turns(self):
        executor = self._bare_executor()
        executor.action_history = [
            {
                "_turn": 0,
                "tool_name": "_react_reflection",
                "arguments": {},
                "result": {"status": "success", "output": "先检查目标文件是否存在"},
                "assistant_content": "先检查目标文件是否存在",
                "reasoning_content": "需要先确认文件状态，避免重复写入",
                "_has_image": False,
                "_image_base64": None,
            },
            {
                "_turn": 1,
                "tool_name": "_assistant_text",
                "arguments": {},
                "result": {"status": "success", "output": "我已经知道下一步要写入目标文件"},
                "assistant_content": "我已经知道下一步要写入目标文件",
                "reasoning_content": "下一步应使用 file_write",
                "_has_image": False,
                "_image_base64": None,
            },
            {
                "_turn": 2,
                "tool_call_id": "call_2_0",
                "tool_name": "file_write",
                "arguments": {"path": "a.txt", "content": "hello"},
                "result": {"status": "success", "output": "ok"},
                "assistant_content": "现在开始写文件",
                "reasoning_content": "工具调用已经准备好",
                "_has_image": False,
                "_image_base64": None,
            },
        ]

        messages = executor._build_messages_from_action_history()
        assistant_texts = [msg.get("content") for msg in messages if msg.get("role") == "assistant"]

        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("执行下一步操作", messages[-1]["content"])
        self.assertTrue(any("先检查目标文件是否存在" in text for text in assistant_texts))
        self.assertTrue(any("我已经知道下一步要写入目标文件" in text for text in assistant_texts))
        self.assertTrue(any("现在开始写文件" in text for text in assistant_texts))
        self.assertTrue(any(msg.get("reasoning_content") == "需要先确认文件状态，避免重复写入" for msg in messages))
        self.assertTrue(any(msg.get("reasoning_content") == "工具调用已经准备好" for msg in messages))

    def test_run_react_reflection_persists_text_to_action_history(self):
        executor = self._bare_executor()
        saved = {"count": 0}
        captured = {}

        def fake_execute_llm_call(*args, **kwargs):
            captured["messages"] = args[1]
            captured["tool_list"] = kwargs.get("tool_list")
            captured["tool_choice"] = kwargs.get("tool_choice")
            return LLMResponse(
                status="success",
                output="先检查最近一次生成的文档并确认需要补写的部分",
                tool_calls=[],
                model="demo-model",
                finish_reason="stop",
                reasoning_content="",
            )

        def fake_save_state(*args, **kwargs):
            saved["count"] += 1

        executor._execute_llm_call = fake_execute_llm_call
        executor._save_state = fake_save_state

        executor._run_react_reflection(
            task_id="/tmp/demo-task",
            task_input="continue task",
            system_prompt="demo prompt",
            messages=[{"role": "user", "content": "请继续"}],
            turn=0,
        )

        self.assertEqual(executor.llm_turn_counter, 1)
        self.assertEqual(saved["count"], 1)
        self.assertEqual(executor.action_history[-1]["tool_name"], "_react_reflection")
        self.assertIn("先检查最近一次生成的文档", executor.action_history[-1]["assistant_content"])
        self.assertEqual(captured["tool_list"], [])
        self.assertEqual(captured["tool_choice"], "none")
        prompt = captured["messages"][-1]["content"]
        self.assertIn("<可用工具详情>", prompt)
        self.assertIn("【file_write】", prompt)
        self.assertIn("path (string, 必需): 文件相对路径", prompt)
        self.assertIn("keyword (string, 可选): 关键词", prompt)

    def test_model_output_payload_prefers_last_execution_turn_over_react_reflection(self):
        executor = self._bare_executor()
        executor.execution_traces = [
            {"debug_label": "execution", "content": "real tool-driving output", "model": "m1"},
            {"debug_label": "react_reflection", "content": "reflection text", "model": "m1"},
        ]
        executor.thinking_traces = []

        payload = executor._build_model_outputs_payload()

        self.assertEqual(payload["last_execution"]["content"], "real tool-driving output")

    def test_run_returns_completed_final_output_history_without_entering_turn_loop(self):
        executor = self._bare_executor()
        final_result = {"status": "success", "output": "already done", "error_information": ""}
        popped = {}

        executor.config_loader.agent_system_name = "demo"
        executor.hierarchy_manager = SimpleNamespace(
            set_runtime_metadata=lambda **_kwargs: None,
            push_agent=lambda _agent_name, _user_input: "agent_1",
            pop_agent=lambda agent_id, output: popped.update({"agent_id": agent_id, "output": output}),
        )
        executor.tool_executor = SimpleNamespace(set_agent_context=lambda **_kwargs: None)
        executor._load_state_from_storage = lambda _task_id: final_result
        executor.max_turns = 100000

        with patch("core.agent_executor.register_running_task"), patch("core.agent_executor.unregister_running_task") as unregister:
            result = executor.run("/tmp/demo-task", "hello")

        self.assertEqual(result, final_result)
        self.assertEqual(popped, {"agent_id": "agent_1", "output": "already done"})
        unregister.assert_called_once_with("/tmp/demo-task")

    def test_react_reflection_stream_maps_content_to_reasoning_tokens(self):
        executor = self._bare_executor()
        captured = []

        def fake_emit(event_type, payload):
            captured.append((event_type, payload))

        executor._emit_sdk_stream_event = fake_emit
        callback = executor._build_llm_stream_callback(
            stream_group="llm",
            agent_name="alpha_agent",
            model="demo-model",
            content_as_reasoning=True,
        )

        callback({
            "kind": "content",
            "text": "react reflection text",
            "model": "demo-model",
            "attempt": 1,
        })

        self.assertEqual(captured[0][0], "run.llm.reasoning_token")
        self.assertEqual(captured[0][1]["token_kind"], "reasoning")


if __name__ == "__main__":
    unittest.main()
