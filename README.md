# InfiAgent

English · [简体中文](README_CN.md)

InfiAgent is a configuration-driven agent framework for long-running tasks. Define agents, prompts, and tools in YAML, run cooperating agents in parallel, and keep conversation state and working files locally.

## Features

- **Long-running execution:** Thinking, ReAct, and ReAct Lite modes, bounded action windows, context compression, and file-based handoffs.
- **Parallel sub-agents:** independent instances, background execution, result collection, cancellation, and parent-child event identities.
- **Resumable sessions:** atomic state writes and durable tool receipts. Interrupted calls with uncertain side effects are reported for inspection instead of silently repeated.
- **Local workspaces:** share project files across sessions while keeping each session's execution records separate.
- **Developer console:** stream runs, inspect tools and sub-agents, answer human interactions, browse files, and configure models.
- **Model roles:** configure execution, thinking, compression, vision, and image generation with shared or separate API connections.
- **Usage panel:** input, output, and cached tokens by hour, day, week, month, or a custom range, with task breakdowns and optional model pricing.
- **Extensible tools:** built-in file, command, search, and document tools, plus custom Python tools, Skills, and MCP.

## Start with Docker

```bash
mkdir -p user_root
docker run -d --name infiagent \
  -p 127.0.0.1:4260:4260 \
  -v "$PWD/user_root:/data/user_root" \
  chenglinhku/mlav3:3.13.0
```

Open [http://localhost:4260](http://localhost:4260). Use **模型 API 配置** in the sidebar to configure your models, choose an Agent System and Agent Name, and select **Stream Send**. **Usage Panel** opens token statistics.

Configuration, sessions, and artifacts persist in the mounted `user_root`. Images are available for `linux/amd64` and `linux/arm64`. A `docker-compose.yml` is also provided.

See the [Docker guide](docs/DOCKER_GUIDE.md) for configuration, workspaces, upgrades, and API usage.

## Python SDK

Python 3.10 or later is required.

```bash
pip install infiagent==3.13.0
infiagent-runtime --user-root ./user_root
```

Configure a model in the browser, then run from the same directory:

```python
from infiagent import infiagent

agent = infiagent(
    user_data_root="./user_root",
    default_agent_system="OpenCowork",
    default_agent_name="alpha_agent",
)
result = agent.run(
    "Inspect the files in this workspace and write a short report.",
    task_id="./user_root/tasks/demo",
)
print(result["output"])
print(result["usage"])
```

Run `infiagent` for the interactive CLI. See the [SDK guide](docs/SDK_GUIDE.md) and [CLI guide](docs/CLI_GUIDE.md).

## Configure an agent

```text
user_root/
  config/llm_config.yaml
  agent_library/my_agent/
    level_0_tools.yaml
    level_3_agents.yaml
    general_prompts.yaml
  tools_library/
  skills/
  tasks/
  workspaces/
```

Define responsibilities, prompts, and `available_tools` in the agent YAML. Define tool descriptions and schemas in the tool YAML. OpenCowork and Researcher provide starting examples.

Use `spawn_subagents`, `collect`, and `cancel` for parallel orchestration. A child-agent call may also set `_background: true`; multiple child-agent calls in one model response run concurrently. Children must be linked in the caller's available tool set.

```json
{
  "tasks": [
    {"agent": "sub_agent", "task_input": "Review the code and tests"},
    {"agent": "sub_agent", "task_input": "Review documentation and examples"}
  ],
  "join": "all"
}
```

## Share a workspace

```python
agent.create_workspace("demo-project", title="Demo project")
agent.run("Prepare the project files", task_id="./user_root/tasks/setup", workspace_id="demo-project")
agent.run("Review those files", task_id="./user_root/tasks/review", workspace_id="demo-project")
```

These sessions share working files and retain separate execution records. Workspaces can also be created and selected in the developer console.

## Beyond the runtime

We are also developing and using **Cosine IDE, an operations console, and a high-availability runtime platform** around InfiAgent. These projects support agent engineering, real-task validation, configuration, and operational workflows. Reusable environments and test cases help agents run tasks, inspect failures, make changes, and verify the results in another run.

See [Project extensions](docs/PLATFORM_OVERVIEW.md) for the overview and current progress.

## Development

```bash
pip install -e .
pip install pytest httpx
python -m pytest tests
python -m build
```

Default tests use temporary directories and local model-service fixtures. Release validation also checks built Python packages, Docker execution, the developer console, and real model calls.

## Paper and license

[InfiAgent: An Infinite-Horizon Framework for General-Purpose Autonomous Agents](https://arxiv.org/abs/2601.03204)

Licensed under [GPL-3.0](LICENSE).

```bibtex
@article{yu2026infiagent,
  title={InfiAgent: An Infinite-Horizon Framework for General-Purpose Autonomous Agents},
  author={Yu, Chenglin and Wang, Yuchen and Wang, Songmiao and Yang, Hongxia and Li, Ming},
  journal={arXiv preprint arXiv:2601.03204},
  year={2026}
}
```
