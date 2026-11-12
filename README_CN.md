# InfiAgent

[English](README.md) · 简体中文

InfiAgent 是一个配置驱动的智能体框架：用 YAML 定义智能体、提示词和工具，让它完成长任务、并行协作，并把会话状态和工作文件保存在本地。

## 主要能力

- **长任务执行**：Thinking、ReAct 与 ReAct Lite 三种运行方式，动作窗口、上下文压缩和文件交接。
- **并行子智能体**：同名多实例、后台执行、结果汇合与取消；父子实例的状态和事件分别记录。
- **可恢复会话**：原子保存状态、保留工具结果回执，中断后可以继续；结果不明的副作用调用会明确报告，交由开发者检查。
- **本地工作区**：多个会话共享代码、数据和产物，各自保留会话记录。
- **开发运行界面**：在浏览器里创建会话、查看流式过程、回答人工交互、管理文件和配置模型。
- **模型配置**：主执行、思考、压缩、读图和文生图分别配置，可共享或单独填写 API 地址与密钥。
- **用量面板**：按小时、天、周、月或自定义时段查看输入、输出、缓存 token 和任务用量；可配置模型单价。
- **工具扩展**：内置文件、命令、检索和文档工具，支持自定义 Python 工具、Skills 与 MCP。

## Docker 快速开始

```bash
mkdir -p user_root
docker run -d --name infiagent \
  -p 127.0.0.1:4260:4260 \
  -v "$PWD/user_root:/data/user_root" \
  chenglinhku/mlav3:3.13.0
```

打开 [http://localhost:4260](http://localhost:4260)。点击左侧 **模型 API 配置**，填写模型与连接信息，然后选择 Agent System 和 Agent Name，创建任务并点击 **Stream Send**。**Usage Panel** 打开用量面板。

工作文件、配置和会话记录保存在挂载的 `user_root` 中。镜像提供 `linux/amd64` 和 `linux/arm64`；也可使用仓库中的 `docker-compose.yml`。

完整操作见 [Docker 与开发界面](docs/DOCKER_GUIDE.md)。

## Python SDK

需要 Python 3.10 或更高版本。

```bash
pip install infiagent==3.13.0
infiagent-runtime --user-root ./user_root
```

先在浏览器中配置模型，再在同一目录运行：

```python
from infiagent import infiagent

agent = infiagent(
    user_data_root="./user_root",
    default_agent_system="OpenCowork",
    default_agent_name="alpha_agent",
)
result = agent.run(
    "整理当前工作区的文件，并写一份说明。",
    task_id="./user_root/tasks/demo",
)
print(result["output"])
print(result["usage"])
```

`infiagent` 命令打开交互式 CLI。更多调用方式见 [SDK 指南](docs/SDK_GUIDE.md) 和 [CLI 指南](docs/CLI_GUIDE.md)。

## 定义智能体

一个最小工程包含：

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

`level_3_agents.yaml` 定义职责、提示词和 `available_tools`；`level_0_tools.yaml` 定义工具说明和参数。随包提供 OpenCowork 与 Researcher 示例，可在它们的基础上修改。

并行编排使用 `spawn_subagents`、`collect` 和 `cancel`。普通子智能体调用支持 `_background: true`；同轮多个子智能体调用也会并行处理。工具名称必须在调用方的可用集合中。

```json
{
  "tasks": [
    {"agent": "sub_agent", "task_input": "检查代码与测试"},
    {"agent": "sub_agent", "task_input": "检查文档与示例"}
  ],
  "join": "all"
}
```

## 共享工作区

```python
agent.create_workspace("demo-project", title="Demo project")
agent.run("准备项目文件", task_id="./user_root/tasks/setup", workspace_id="demo-project")
agent.run("检查这些文件", task_id="./user_root/tasks/review", workspace_id="demo-project")
```

两个会话操作同一份工作文件，运行记录分别保存。开发界面也可以在新建会话时创建或选择工作区。

## 项目延伸

围绕 InfiAgent，我们也在开发和使用 **Cosine IDE、运维控制台与高可用运行平台**，支持智能体工程开发、真实任务验证、配置与运行管理等工作。我们也在这些工程中复用运行环境和测试样例，通过“执行任务 → 定位问题 → 修改 → 复测”持续改进智能体。

图文与当前进展见 [项目延伸](docs/PLATFORM_OVERVIEW.md)。

## 开发与测试

```bash
pip install -e .
pip install pytest httpx
python -m pytest tests
python -m build
```

默认测试使用临时目录和本地模型服务替身。发布验证还检查构建后的 Python 包、Docker 运行界面及实际模型调用。

## 论文与许可

[InfiAgent: An Infinite-Horizon Framework for General-Purpose Autonomous Agents](https://arxiv.org/abs/2601.03204)

本项目采用 [GPL-3.0](LICENSE)。
