#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import signal
import json
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from infiagent import infiagent
from core.context_builder import ContextBuilder
from core.hierarchy_manager import get_hierarchy_manager
from tool_server_lite.tools.skill_tools import FreshTool
from tool_server_lite.tools.task_tools import AddMessageTool, ListTaskIdsTool, TaskShareContextPathTool
from utils.config_loader import ConfigLoader
from utils.runtime_control import pop_fresh_request, register_running_task, unregister_running_task
from utils.task_runtime import append_task_message, reset_task_state
from utils.user_paths import get_runtime_settings, runtime_env_scope


class SDKRuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)

    def test_default_root_semantics_follow_current_env(self):
        root = (self.base / "default_root").resolve()
        task_id = str((self.base / "default_task").resolve())

        with runtime_env_scope({"MLA_USER_DATA_ROOT": str(root)}):
            agent = infiagent()
            result = agent.add_message("default root message", task_id=task_id)
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["share_context_path"].startswith(str(root / "conversations")))

            listed = agent.list_task_ids()
            self.assertTrue(any(item["task_id"] == task_id for item in listed["tasks"]))

            share_paths = agent.task_share_context_path(task_id=task_id)
            self.assertTrue(share_paths["share_context_path"].startswith(str(root / "conversations")))
            self.assertTrue(share_paths["stack_path"].startswith(str(root / "conversations")))
            self.assertTrue((root / "config" / "app_config.json").exists())

    def test_custom_user_data_root_applies_to_sdk_and_runtime_control(self):
        root = (self.base / "custom_root").resolve()
        task_id = str((self.base / "custom_task").resolve())
        agent = infiagent(user_data_root=str(root))

        result = agent.add_message("sdk scoped message", task_id=task_id)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["share_context_path"].startswith(str(root / "conversations")))

        with agent._runtime_scope():
            register_running_task(task_id, "alpha_agent", "hello", "OpenCowork")
            try:
                fresh_result = agent.fresh(task_id=task_id, reason="sdk-test-fresh")
                self.assertEqual(fresh_result["status"], "success")
                runtime_root = root / "runtime"
                self.assertTrue((runtime_root / "running_tasks").exists())
                self.assertEqual(pop_fresh_request(task_id), "sdk-test-fresh")
            finally:
                unregister_running_task(task_id)

    def test_task_tools_follow_custom_user_data_root(self):
        root = (self.base / "tool_root").resolve()
        task_id = str((self.base / "tool_task").resolve())

        with runtime_env_scope({"MLA_USER_DATA_ROOT": str(root)}):
            add_tool = AddMessageTool()
            add_result = add_tool.execute(task_id, {"message": "tool message", "source": "agent"})
            self.assertEqual(add_result["status"], "success")
            self.assertTrue(add_result["share_context_path"].startswith(str(root / "conversations")))

            path_tool = TaskShareContextPathTool()
            path_result = path_tool.execute(task_id, {})
            self.assertEqual(path_result["status"], "success")
            self.assertTrue(path_result["share_context_path"].startswith(str(root / "conversations")))

            list_tool = ListTaskIdsTool()
            list_result = list_tool.execute(task_id, {})
            self.assertEqual(list_result["status"], "success")
            self.assertTrue(any(item["task_id"] == task_id for item in list_result["tasks"]))

            fresh_tool = FreshTool()
            fresh_signal = fresh_tool.execute(task_id, {})
            self.assertEqual(fresh_signal["status"], "success")
            self.assertEqual(fresh_signal["_fresh_task_id"], task_id)

    def test_user_data_root_alone_is_enough_for_agent_library_loading(self):
        root = (self.base / "config_root").resolve()
        agent = infiagent(user_data_root=str(root))

        with agent._runtime_scope():
            loader = ConfigLoader("OpenCowork")
            config = loader.get_tool_config("alpha_agent")
            for tool_name in [
                "fresh",
                "add_message",
                "start_background_task",
                "task_share_context_path",
                "list_task_ids",
            ]:
                self.assertIn(tool_name, loader.all_tools)

        self.assertEqual(config.get("type"), "llm_call_agent")
        self.assertTrue((root / "agent_library" / "OpenCowork").exists())

    def test_sdk_requires_explicit_task_id(self):
        agent = infiagent()
        with self.assertRaises(ValueError):
            agent.run("missing task id", task_id="")

    def test_sdk_instances_do_not_leak_user_data_roots(self):
        root_a = (self.base / "root_a").resolve()
        root_b = (self.base / "root_b").resolve()
        task_a = str((self.base / "task_a").resolve())
        task_b = str((self.base / "task_b").resolve())

        agent_a = infiagent(user_data_root=str(root_a))
        agent_b = infiagent(user_data_root=str(root_b))

        result_a = agent_a.add_message("message for a", task_id=task_a)
        result_b = agent_b.add_message("message for b", task_id=task_b)

        self.assertTrue(result_a["share_context_path"].startswith(str(root_a / "conversations")))
        self.assertTrue(result_b["share_context_path"].startswith(str(root_b / "conversations")))

        list_a = agent_a.list_task_ids()
        list_b = agent_b.list_task_ids()

        self.assertTrue(any(item["task_id"] == task_a for item in list_a["tasks"]))
        self.assertFalse(any(item["task_id"] == task_b for item in list_a["tasks"]))
        self.assertTrue(any(item["task_id"] == task_b for item in list_b["tasks"]))
        self.assertFalse(any(item["task_id"] == task_a for item in list_b["tasks"]))

    def test_runtime_settings_fold_legacy_thinking_fields_into_action_window(self):
        root = (self.base / "legacy_window_root").resolve()
        with runtime_env_scope({
            "MLA_USER_DATA_ROOT": str(root),
            "MLA_ACTION_WINDOW_STEPS": None,
            "MLA_THINKING_STEPS": "2",
            "MLA_THINKING_INTERVAL": "5",
        }):
            runtime = get_runtime_settings()

        self.assertEqual(runtime["action_window_steps"], 5)
        self.assertEqual(runtime["thinking_steps"], 5)
        self.assertEqual(runtime["thinking_interval"], 5)

    def test_sdk_maps_legacy_window_overrides_to_single_canonical_value(self):
        root = (self.base / "sdk_window_root").resolve()
        agent = infiagent(
            user_data_root=str(root),
            action_window_steps=3,
            thinking_interval=4,
            thinking_steps=7,
        )

        self.assertEqual(agent.runtime_env_overrides["MLA_ACTION_WINDOW_STEPS"], "7")
        self.assertEqual(agent.runtime_env_overrides["MLA_THINKING_INTERVAL"], "7")
        self.assertEqual(agent.runtime_env_overrides["MLA_THINKING_STEPS"], "7")
        self.assertEqual(agent.action_window_steps, 7)

    def test_run_returns_busy_when_task_already_running(self):
        root = (self.base / "busy_root").resolve()
        task_id = str((self.base / "busy_task").resolve())
        agent = infiagent(user_data_root=str(root))
        with agent._runtime_scope():
            register_running_task(task_id, "alpha_agent", "hello", "OpenCowork")
            try:
                result = agent.run("new request", task_id=task_id)
            finally:
                unregister_running_task(task_id)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["task_id"], task_id)

    def test_background_task_launch_uses_user_data_root(self):
        root = (self.base / "launch_root").resolve()
        task_id = str((self.base / "launch_task").resolve())
        llm_config = str((Path(__file__).resolve().parent / "llm_config_dummy.yaml").resolve())
        agent = infiagent(user_data_root=str(root), llm_config_path=llm_config)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            result = agent.start_background_task(
                task_id=task_id,
                user_input="background launch smoke test",
                force_new=True,
            )
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["log_path"].startswith(str(root / "runtime" / "launched_tasks")))

        pid = result.get("pid")
        self.assertIsInstance(pid, int)
        import psutil
        try:
            process = psutil.Process(pid)
            process.terminate()
            process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass

    def test_task_snapshot_falls_back_to_history_final_output(self):
        root = (self.base / "history_root").resolve()
        task_id = str((self.base / "history_task").resolve())
        agent = infiagent(user_data_root=str(root))

        with agent._runtime_scope():
            manager = get_hierarchy_manager(task_id)
            context = manager._load_context()
            context["history"] = [{
                "instructions": [],
                "hierarchy": {"worker_agent_demo": {"parent": None, "children": [], "level": 0}},
                "agents_status": {
                    "worker_agent_demo": {
                        "agent_name": "worker_agent",
                        "status": "completed",
                        "thinking_updated_at": "2026-03-10T10:00:00+08:00",
                        "latest_thinking": "done thinking",
                        "final_output": "done output",
                        "end_time": "2026-03-10T10:05:00+08:00",
                    }
                },
                "start_time": "2026-03-10T10:00:00+08:00",
                "completion_time": "2026-03-10T10:05:00+08:00",
            }]
            context["current"] = {
                "instructions": [],
                "hierarchy": {},
                "agents_status": {},
                "start_time": "2026-03-10T10:06:00+08:00",
                "last_updated": "2026-03-10T10:06:00+08:00",
            }
            manager._save_context(context)

        snapshot = agent.task_snapshot(task_id=task_id)
        self.assertEqual(snapshot["status"], "success")
        self.assertEqual(snapshot["last_final_output"], "done output")
        self.assertEqual(snapshot["last_final_output_at"], "2026-03-10T10:05:00+08:00")
        self.assertEqual(snapshot["latest_thinking"], "done thinking")

    def test_tool_hooks_are_exposed_in_launch_config(self):
        callback = str((self.base / "hook.py").resolve()) + ":on_tool_event"
        hooks = [{
            "name": "demo",
            "when": "after",
            "tool_names": ["final_output"],
            "callback": callback,
            "result_filters": {"status": "success"},
        }]
        agent = infiagent(user_data_root=str((self.base / "hook_root").resolve()), tool_hooks=hooks)
        launch_config = agent._build_launch_config()
        self.assertEqual(launch_config["tool_hooks"], hooks)

    def test_tool_runtime_defaults_are_exposed_in_launch_config(self):
        defaults = {
            "file_write": {
                "mode": "write",
                "workspace_root": "/tmp/demo",
            }
        }
        agent = infiagent(
            user_data_root=str((self.base / "tool_defaults_root").resolve()),
            tool_runtime_defaults=defaults,
        )
        launch_config = agent._build_launch_config()
        self.assertEqual(launch_config["tool_runtime_defaults"], defaults)

    def test_context_hooks_are_exposed_in_launch_config(self):
        callback = str((self.base / "ctx_hook.py").resolve()) + ":on_context"
        hooks = [{
            "name": "ctx-demo",
            "when": "after_build",
            "callback": callback,
        }]
        agent = infiagent(user_data_root=str((self.base / "ctx_hook_root").resolve()), context_hooks=hooks)
        launch_config = agent._build_launch_config()
        self.assertEqual(launch_config["context_hooks"], hooks)
        self.assertTrue(launch_config["seed_builtin_resources"])

    def test_framework_default_skill_tools_are_injected(self):
        loader = ConfigLoader("OpenCowork")
        self.assertIn("load_skill", loader.all_tools)
        self.assertIn("offload_skill", loader.all_tools)

    def test_tool_runtime_defaults_log_mode_is_exposed_in_launch_config(self):
        agent = infiagent(
            user_data_root=str((self.base / "log_mode_root").resolve()),
            tool_runtime_defaults={"file_write": {"content": "secret"}},
            tool_runtime_defaults_log_mode="fingerprint",
        )
        launch_config = agent._build_launch_config()
        self.assertEqual(launch_config["tool_runtime_defaults_log_mode"], "fingerprint")

    def test_max_turns_is_exposed_in_launch_config(self):
        agent = infiagent(
            user_data_root=str((self.base / "max_turns_root").resolve()),
            max_turns=321,
        )
        launch_config = agent._build_launch_config()
        self.assertEqual(launch_config["max_turns"], 321)
        runtime = agent.describe_runtime()
        self.assertEqual(runtime["max_turns"], 321)

    def test_pause_task_aliases_reset_semantics(self):
        agent = infiagent(user_data_root=str((self.base / "pause_root").resolve()))
        task_id = str((self.base / "pause_task").resolve())

        with patch("infiagent.sdk.reset_task_state", return_value=(True, {"task_id": task_id, "preserve_history": True})) as reset_mock:
            result = agent.pause_task(task_id=task_id, reason="pause please")

        self.assertEqual(result["status"], "success")
        reset_mock.assert_called_once()
        kwargs = reset_mock.call_args.kwargs
        self.assertEqual(kwargs["task_id"], task_id)
        self.assertEqual(kwargs["reason"], "pause please")
        self.assertTrue(kwargs["preserve_history"])

    def test_run_can_materialize_system_add_content(self):
        root = (self.base / "system_add_root").resolve()
        task_id = str((self.base / "system_add_task").resolve())
        agent = infiagent(user_data_root=str(root))

        with patch("infiagent.sdk.is_task_running", return_value=True):
            result = agent.run(
                "noop",
                task_id=task_id,
                system_add_content="SYSTEM_ADD_MARKER",
            )

        self.assertEqual(result["status"], "busy")
        self.assertEqual((Path(task_id) / "system-add.md").read_text(encoding="utf-8"), "SYSTEM_ADD_MARKER")

    def test_run_can_materialize_system_add_directory(self):
        root = (self.base / "system_add_dir_root").resolve()
        task_id = str((self.base / "system_add_dir_task").resolve())
        source_dir = (self.base / "system_add_src").resolve()
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "system-add.md").write_text("DEFAULT_MARKER", encoding="utf-8")
        (source_dir / "system-add.alpha_agent.md").write_text("AGENT_MARKER", encoding="utf-8")
        agent = infiagent(user_data_root=str(root))

        with patch("infiagent.sdk.is_task_running", return_value=True):
            result = agent.run(
                "noop",
                task_id=task_id,
                system_add_path=str(source_dir),
            )

        self.assertEqual(result["status"], "busy")
        self.assertEqual((Path(task_id) / "system-add.md").read_text(encoding="utf-8"), "DEFAULT_MARKER")
        self.assertEqual((Path(task_id) / "system-add.alpha_agent.md").read_text(encoding="utf-8"), "AGENT_MARKER")

    def test_run_can_materialize_agent_specific_system_add_file(self):
        root = (self.base / "system_add_file_root").resolve()
        task_id = str((self.base / "system_add_file_task").resolve())
        source_file = (self.base / "system-add.alpha_agent.md").resolve()
        source_file.write_text("AGENT_ONLY_MARKER", encoding="utf-8")
        agent = infiagent(user_data_root=str(root))

        with patch("infiagent.sdk.is_task_running", return_value=True):
            result = agent.run(
                "noop",
                task_id=task_id,
                system_add_path=str(source_file),
            )

        self.assertEqual(result["status"], "busy")
        self.assertEqual((Path(task_id) / "system-add.alpha_agent.md").read_text(encoding="utf-8"), "AGENT_ONLY_MARKER")

    def test_context_builder_prefers_agent_name_specific_system_add(self):
        task_dir = (self.base / "context_system_add_task").resolve()
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "system-add.md").write_text("DEFAULT_MARKER", encoding="utf-8")
        (task_dir / "system-add.alpha_agent.md").write_text("ALPHA_MARKER", encoding="utf-8")

        builder = ContextBuilder.__new__(ContextBuilder)
        builder.config_loader = type("Loader", (), {"agent_system_name": "ExampleSystem"})()

        result = builder._build_task_system_add(str(task_dir), "alpha_agent")
        self.assertIn("ALPHA_MARKER", result)
        self.assertNotIn("DEFAULT_MARKER", result)

    def test_add_message_resume_if_needed_uses_resume_when_stack_exists(self):
        root = (self.base / "resume_root").resolve()
        task_id = str((self.base / "resume_task").resolve())

        with runtime_env_scope({"MLA_USER_DATA_ROOT": str(root)}):
            manager = get_hierarchy_manager(task_id)
            manager.set_runtime_metadata(
                agent_system="OpenCowork",
                agent_name="alpha_agent",
                user_input="previous input",
            )
            manager._save_stack([{
                "agent_id": "alpha_agent_demo",
                "agent_name": "alpha_agent",
                "parent_id": None,
                "level": 0,
                "user_input": "previous input",
                "start_time": "2026-03-25T10:00:00+08:00",
            }])

            with patch("utils.task_runtime.resume_task_with_fresh", return_value=(True, "resumed")) as resume_mock:
                with patch("utils.task_runtime.launch_task_process") as launch_mock:
                    ok, payload = append_task_message(
                        task_id=task_id,
                        message="resume me",
                        source="user",
                        resume_if_needed=True,
                        fallback_agent_system="OpenCowork",
                    )

        self.assertTrue(ok)
        self.assertTrue(payload["resumed"])
        self.assertFalse(payload["launched"])
        resume_mock.assert_called_once()
        launch_mock.assert_not_called()

    def test_add_message_resume_if_needed_launches_new_task_when_stack_empty(self):
        root = (self.base / "launch_after_add_root").resolve()
        task_id = str((self.base / "launch_after_add_task").resolve())

        with runtime_env_scope({"MLA_USER_DATA_ROOT": str(root), "MLA_MAX_TURNS": "77"}):
            manager = get_hierarchy_manager(task_id)
            manager.set_runtime_metadata(
                agent_system="OpenCowork",
                agent_name="alpha_agent",
                user_input="previous input",
            )
            manager._save_stack([])

            with patch("utils.task_runtime.launch_task_process", return_value=(True, {
                "message": f"已在后台启动任务: {task_id}",
                "task_id": task_id,
                "pid": 4321,
                "log_path": str(root / "runtime" / "launched_tasks" / "demo.log"),
                "agent_system": "OpenCowork",
                "agent_name": "alpha_agent",
            })) as launch_mock:
                ok, payload = append_task_message(
                    task_id=task_id,
                    message="please continue",
                    source="user",
                    resume_if_needed=True,
                    fallback_agent_system="OpenCowork",
                    env_overrides={"MLA_USER_DATA_ROOT": str(root), "MLA_MAX_TURNS": "77"},
                )

        self.assertTrue(ok)
        self.assertFalse(payload["resumed"])
        self.assertTrue(payload["launched"])
        launch_mock.assert_called_once()
        kwargs = launch_mock.call_args.kwargs
        self.assertEqual(kwargs["task_id"], task_id)
        self.assertEqual(kwargs["user_input"], "please continue")
        self.assertEqual(kwargs["agent_system"], "OpenCowork")
        self.assertEqual(kwargs["agent_name"], "alpha_agent")
        self.assertEqual(kwargs["config"]["user_data_root"], str(root))
        self.assertEqual(kwargs["config"]["max_turns"], 77)

    def test_concurrent_add_message_keeps_all_instructions(self):
        root = (self.base / "concurrent_add_root").resolve()
        task_id = str((self.base / "concurrent_add_task").resolve())
        release_file = self.base / "release_concurrent_add"
        backend_root = Path(__file__).resolve().parents[1]
        worker = (
            "import os, time\n"
            "from utils.task_runtime import append_task_message\n"
            "while not os.path.exists(os.environ['RELEASE_FILE']):\n"
            "    time.sleep(0.01)\n"
            "ok, payload = append_task_message(\n"
            "    os.environ['TASK_ID'], os.environ['MSG'], source='test'\n"
            ")\n"
            "raise SystemExit(0 if ok else 1)\n"
        )

        with runtime_env_scope({"MLA_USER_DATA_ROOT": str(root)}):
            ok, _payload = append_task_message(task_id, "warmup", source="test")
            self.assertTrue(ok)

        env_base = os.environ.copy()
        env_base["MLA_USER_DATA_ROOT"] = str(root)
        env_base["TASK_ID"] = task_id
        env_base["RELEASE_FILE"] = str(release_file)
        env_base["PYTHONPATH"] = (
            str(backend_root)
            + os.pathsep
            + env_base.get("PYTHONPATH", "")
        )

        process_count = 24
        processes = []
        outputs = []
        for index in range(process_count):
            env = env_base.copy()
            env["MSG"] = f"race-{index}"
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", worker],
                    cwd=str(backend_root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        try:
            time.sleep(0.2)
            release_file.write_text("go", encoding="utf-8")
            outputs = [process.communicate(timeout=20) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        failures = [
            (process.returncode, stdout, stderr)
            for process, (stdout, stderr) in zip(processes, outputs)
            if process.returncode != 0
        ]
        self.assertEqual(failures, [])

        with runtime_env_scope({"MLA_USER_DATA_ROOT": str(root)}):
            manager = get_hierarchy_manager(task_id)
            instructions = manager._load_context()["current"]["instructions"]

        messages = [item.get("instruction") for item in instructions]
        self.assertEqual(len(messages), process_count + 1)
        self.assertEqual(len(set(messages)), process_count + 1)
        self.assertEqual(
            set(messages),
            {"warmup", *{f"race-{index}" for index in range(process_count)}},
        )

    def test_pause_then_add_message_does_not_leave_dirty_stack(self):
        root = (self.base / "pause_resume_root").resolve()
        task_id = str((self.base / "pause_resume_task").resolve())

        with runtime_env_scope({"MLA_USER_DATA_ROOT": str(root)}):
            manager = get_hierarchy_manager(task_id)
            manager.set_runtime_metadata(
                agent_system="OpenCowork",
                agent_name="alpha_agent",
                user_input="previous input",
            )
            context = manager._load_context()
            context["current"] = {
                "instructions": [{"id": "old_instruction", "message": "old"}],
                "hierarchy": {"alpha_agent_demo": {"level": 0}},
                "agents_status": {"alpha_agent_demo": {"agent_name": "alpha_agent", "status": "running"}},
                "start_time": "2026-04-10T10:00:00",
                "last_updated": "2026-04-10T10:00:00",
            }
            manager._save_context(context)
            manager._save_stack([{
                "agent_id": "alpha_agent_demo",
                "agent_name": "alpha_agent",
                "parent_id": None,
                "level": 0,
                "user_input": "previous input",
                "start_time": "2026-04-10T10:00:00",
            }])

            ok, reset_payload = reset_task_state(
                task_id=task_id,
                preserve_history=True,
                kill_background_processes=False,
                reason="manual pause",
            )

            self.assertTrue(ok)
            self.assertEqual(manager._load_stack(), [])
            paused_context = manager._load_context()
            self.assertEqual(paused_context["current"]["instructions"], [])
            self.assertEqual(len(paused_context["history"]), 1)

            with patch("utils.task_runtime.launch_task_process", return_value=(True, {
                "message": f"已在后台启动任务: {task_id}",
                "task_id": task_id,
                "pid": 9876,
                "log_path": str(root / "runtime" / "launched_tasks" / "resume.log"),
                "agent_system": "OpenCowork",
                "agent_name": "alpha_agent",
            })) as launch_mock:
                ok, payload = append_task_message(
                    task_id=task_id,
                    message="continue from pause",
                    source="user",
                    resume_if_needed=True,
                    fallback_agent_system="OpenCowork",
                    env_overrides={"MLA_USER_DATA_ROOT": str(root)},
                )

            self.assertTrue(ok)
            self.assertTrue(payload["launched"])
            self.assertFalse(payload["resumed"])
            launch_mock.assert_called_once()

            final_context = manager._load_context()
            self.assertEqual(len(final_context["history"]), 1)
            self.assertEqual(len(final_context["current"]["instructions"]), 1)
            self.assertEqual(manager._load_stack(), [])
            self.assertEqual(reset_payload["task_id"], task_id)


if __name__ == "__main__":
    unittest.main()
