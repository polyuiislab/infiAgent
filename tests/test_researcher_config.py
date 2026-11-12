#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path

import yaml


RESEARCHER_ROOT = Path(__file__).resolve().parents[1] / "config" / "agent_library" / "Researcher"


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_researcher_all_agents_have_execute_command():
    for filename in ["level_1_agents.yaml", "level_2_agents.yaml", "level_3_agents.yaml", "level_-1_judge_agent.yaml"]:
        payload = _load_yaml(RESEARCHER_ROOT / filename)
        for agent_name, config in (payload.get("tools") or {}).items():
            if not isinstance(config, dict):
                continue
            if config.get("type") != "llm_call_agent":
                continue
            available_tools = config.get("available_tools") or []
            assert "execute_command" in available_tools, f"{filename}:{agent_name} is missing execute_command"


def test_only_top_level_alpha_agent_has_judge_agent():
    for filename in ["level_1_agents.yaml", "level_2_agents.yaml", "level_3_agents.yaml"]:
        payload = _load_yaml(RESEARCHER_ROOT / filename)
        for agent_name, config in (payload.get("tools") or {}).items():
            if not isinstance(config, dict):
                continue
            if config.get("type") != "llm_call_agent":
                continue
            available_tools = config.get("available_tools") or []
            if agent_name == "alpha_agent" and filename == "level_3_agents.yaml":
                assert "judge_agent" in available_tools
            else:
                assert "judge_agent" not in available_tools, f"{filename}:{agent_name} should not directly call judge_agent"


def test_researcher_judge_agent_has_strict_review_tools_and_prompt():
    payload = _load_yaml(RESEARCHER_ROOT / "level_-1_judge_agent.yaml")
    judge = payload["tools"]["judge_agent"]
    available = set(judge.get("available_tools") or [])
    assert {"dir_list", "file_read", "reference_list", "parse_document", "image_read", "execute_command", "final_output"} <= available

    workflow = (judge.get("prompts") or {}).get("agent_workflow", "")
    assert "review 类型文稿必须满足的硬性标准" in workflow
    assert "方法类文稿必须满足的硬性标准" in workflow
    assert ">= 50" in workflow
    assert ">= 0.8" in workflow
    assert "reference.bib 中是否包含中文字符" in workflow


def test_researcher_alpha_prompt_requires_judge_review():
    payload = _load_yaml(RESEARCHER_ROOT / "level_3_agents.yaml")
    alpha = payload["tools"]["alpha_agent"]
    workflow = (alpha.get("prompts") or {}).get("agent_workflow", "")
    assert "必须调用 judge_agent" in workflow
