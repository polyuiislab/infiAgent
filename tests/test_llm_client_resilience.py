#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services.llm_client import LLMResponse, SimpleLLMClient
from utils.user_paths import get_task_file_prefix


class _DummyDelta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls or []


class _DummyChoice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta or _DummyDelta()
        self.finish_reason = finish_reason


class _DummyChunk:
    def __init__(self, *, content=None, reasoning_content=None, finish_reason=None, model="demo-model", usage=None):
        self.model = model
        self.usage = usage
        self.choices = [_DummyChoice(delta=_DummyDelta(content=content, reasoning_content=reasoning_content), finish_reason=finish_reason)]


class LLMClientResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_user_data_root = os.environ.get("MLA_USER_DATA_ROOT")
        os.environ["MLA_USER_DATA_ROOT"] = self.temp_dir.name
        self.addCleanup(self._restore_user_data_root)

        llm_config_path = Path(__file__).parent / "llm_config_dummy.yaml"
        self.client = SimpleLLMClient(llm_config_path=str(llm_config_path))

    def _restore_user_data_root(self):
        if self.original_user_data_root is None:
            os.environ.pop("MLA_USER_DATA_ROOT", None)
        else:
            os.environ["MLA_USER_DATA_ROOT"] = self.original_user_data_root

    def test_first_chunk_timeout_returns_quickly_without_waiting_for_worker(self):
        self.client.first_chunk_timeout = 0.05
        self.client.timeout = 1
        self.client.stream_timeout = 1

        release_worker = threading.Event()
        worker_finished = threading.Event()

        def slow_completion(**kwargs):
            release_worker.wait(5)
            worker_finished.set()
            return iter(())

        try:
            with patch("services.llm_client.completion", side_effect=slow_completion):
                with patch.object(self.client, "_append_debug_record", lambda **kwargs: None):
                    response = self.client._chat_internal(
                        history=[], model="openai/gpt-4o-mini", system_prompt="system",
                        tool_list=[], tool_choice=None, temperature=0, max_tokens=0,
                    )
            self.assertEqual(response.status, "error")
            self.assertEqual(response.finish_reason, "timeout")
            self.assertFalse(worker_finished.is_set(), "timeout waited for the stalled worker")
        finally:
            release_worker.set()

    def test_non_retriable_error_stops_retry_loop_early(self):
        attempts = []

        def fake_chat_internal(*args):
            attempts.append(len(attempts) + 1)
            return LLMResponse(
                status="error",
                output="",
                tool_calls=[],
                model="openai/gpt-4o-mini",
                finish_reason="error",
                error_information="Invalid API key provided by upstream",
            )

        with patch.object(self.client, "_chat_internal", side_effect=fake_chat_internal):
            with patch("services.llm_client.time.sleep", lambda *_: None):
                with self.assertRaises(Exception) as ctx:
                    self.client.chat(
                        history=[],
                        model="openai/gpt-4o-mini",
                        system_prompt="system",
                        tool_list=[],
                        tool_choice=None,
                        max_retries=3,
                    )

        self.assertEqual(attempts, [1])
        self.assertIn("不可重试", str(ctx.exception))

    def test_retry_emits_stream_reset_before_second_attempt(self):
        streamed = []
        attempt_counter = {"value": 0}

        def fake_chat_internal(*args):
            stream_callback = args[-1]
            attempt_counter["value"] += 1
            attempt_index = attempt_counter["value"]
            if attempt_index == 1:
                stream_callback({
                    "kind": "content",
                    "text": "partial",
                    "model": "demo-model",
                    "debug_label": "execution",
                    "attempt": attempt_index,
                })
                return LLMResponse(
                    status="error",
                    output="",
                    tool_calls=[],
                    model="demo-model",
                    finish_reason="timeout",
                    error_information="request timed out",
                )

            stream_callback({
                "kind": "content",
                "text": "final",
                "model": "demo-model",
                "debug_label": "execution",
                "attempt": attempt_index,
            })
            return LLMResponse(
                status="success",
                output="done",
                tool_calls=[],
                model="demo-model",
                finish_reason="stop",
            )

        with patch.object(self.client, "_chat_internal", side_effect=fake_chat_internal):
            with patch("services.llm_client.time.sleep", lambda *_: None):
                response = self.client.chat(
                    history=[],
                    model="openai/gpt-4o-mini",
                    system_prompt="system",
                    tool_list=[],
                    tool_choice=None,
                    max_retries=1,
                    stream_callback=streamed.append,
                )

        self.assertEqual(response.status, "success")
        self.assertEqual(streamed[0]["kind"], "content")
        self.assertEqual(streamed[0]["attempt"], 1)
        self.assertEqual(streamed[1]["kind"], "content")
        self.assertEqual(streamed[1]["attempt"], 2)

    def test_stream_timeout_after_first_chunk_returns_quickly(self):
        self.client.first_chunk_timeout = 1
        self.client.stream_timeout = 0.05
        self.client.timeout = 1

        release_worker = threading.Event()
        worker_finished = threading.Event()

        def slow_stream_completion(**kwargs):
            def _iterator():
                yield _DummyChunk(content="hello")
                release_worker.wait(5)
                worker_finished.set()
                yield _DummyChunk(content="world", finish_reason="stop")
            return _iterator()

        try:
            with patch("services.llm_client.completion", side_effect=slow_stream_completion):
                with patch.object(self.client, "_append_debug_record", lambda **kwargs: None):
                    response = self.client._chat_internal(
                        history=[], model="openai/gpt-4o-mini", system_prompt="system",
                        tool_list=[], tool_choice=None, temperature=0, max_tokens=0,
                    )
            self.assertEqual(response.status, "error")
            self.assertEqual(response.finish_reason, "timeout")
            self.assertIn("流式输出超时", response.error_information)
            self.assertFalse(worker_finished.is_set(), "timeout waited for the stalled stream")
        finally:
            release_worker.set()

    def test_outbound_history_keeps_native_reasoning_by_default(self):
        captured = {}

        def fake_completion(**kwargs):
            captured["messages"] = kwargs["messages"]
            return iter([_DummyChunk(content="ok", finish_reason="stop")])

        history = [
            {
                "role": "assistant",
                "content": "上一轮已经完成目录检查",
                "reasoning_content": "下一轮需要读取文件内容",
            }
        ]

        with patch("services.llm_client.completion", side_effect=fake_completion):
            with patch.object(self.client, "_append_debug_record", lambda **kwargs: None):
                response = self.client._chat_internal(
                    history=history,
                    model="openai/gpt-4o-mini",
                    system_prompt="system",
                    tool_list=[],
                    tool_choice=None,
                    temperature=0,
                    max_tokens=0,
                )

        self.assertEqual(response.status, "success")
        outbound_assistant = captured["messages"][1]
        self.assertEqual(outbound_assistant["role"], "assistant")
        self.assertEqual(outbound_assistant["content"], "上一轮已经完成目录检查")
        self.assertEqual(outbound_assistant["reasoning_content"], "下一轮需要读取文件内容")

    def test_reasoning_history_fallback_merges_reasoning_for_rest_of_run(self):
        captured = []

        def fake_completion(**kwargs):
            captured.append(kwargs["messages"])
            if len(captured) == 1:
                raise Exception("Invalid assistant message: content or tool_calls must be set")
            return iter([_DummyChunk(content="ok", finish_reason="stop")])

        history = [
            {
                "role": "assistant",
                "content": "上一轮已经完成目录检查",
                "reasoning_content": "下一轮需要读取文件内容",
            }
        ]

        with patch("services.llm_client.completion", side_effect=fake_completion):
            with patch.object(self.client, "_append_debug_record", lambda **kwargs: None):
                response = self.client._chat_internal(
                    history=history,
                    model="openai/gpt-4o-mini",
                    system_prompt="system",
                    tool_list=[],
                    tool_choice=None,
                    temperature=0,
                    max_tokens=0,
                )

        self.assertEqual(response.status, "success")
        self.assertEqual(self.client.reasoning_history_mode, "content")
        retry_assistant = captured[1][1]
        self.assertNotIn("reasoning_content", retry_assistant)
        self.assertIn("上一轮已经完成目录检查", retry_assistant["content"])
        self.assertIn("下一轮需要读取文件内容", retry_assistant["content"])

    def test_tool_request_appends_user_after_assistant_prefill(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": "reflection"},
        ]
        tools = [{"type": "function", "function": {"name": "dir_list", "parameters": {}}}]

        fixed = self.client._ensure_tool_request_not_assistant_prefill(messages, tools)

        self.assertEqual(fixed[-1]["role"], "user")
        self.assertIn("不要把上一条 assistant 内容当作待续写前缀", fixed[-1]["content"])
        self.assertEqual(messages[-1]["role"], "assistant")

    def test_chat_internal_writes_raw_litellm_io_training_trace(self):
        task_id = str(Path(self.temp_dir.name) / "workspace" / "trace-demo")

        def fake_completion(**kwargs):
            return iter([
                _DummyChunk(
                    content="final answer",
                    reasoning_content="model reasoning",
                    finish_reason="stop",
                    model="openai/gpt-4o-mini",
                )
            ])

        with patch("services.llm_client.completion", side_effect=fake_completion):
            with patch.object(self.client, "_append_debug_record", lambda **kwargs: None):
                response = self.client._chat_internal(
                    history=[{"role": "user", "content": "hello"}],
                    model="openai/gpt-4o-mini",
                    system_prompt="system prompt",
                    tool_list=[],
                    tool_choice=None,
                    temperature=0,
                    max_tokens=0,
                    debug_task_id=task_id,
                    debug_label="execution",
                )

        self.assertEqual(response.status, "success")
        trace_file = Path(self.temp_dir.name) / "training_traces" / f"{get_task_file_prefix(task_id)}_raw_io.jsonl"
        self.assertTrue(trace_file.exists())
        records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)

        record = records[0]
        self.assertEqual(record["kind"], "llm.io")
        self.assertEqual(record["debug_label"], "execution")
        self.assertEqual(record["request"]["messages"][0]["role"], "system")
        self.assertEqual(record["request"]["messages"][0]["content"], "system prompt")
        self.assertEqual(record["request"]["messages"][1]["role"], "user")
        self.assertEqual(record["request"]["messages"][1]["content"], "hello")
        self.assertNotIn("api_key", json.dumps(record["request"], ensure_ascii=False))
        self.assertEqual(record["response"]["message"]["content"], "final answer")
        self.assertEqual(record["response"]["message"]["reasoning_content"], "model reasoning")
        self.assertEqual(record["response"]["message"]["tool_calls"], [])


if __name__ == "__main__":
    unittest.main()
