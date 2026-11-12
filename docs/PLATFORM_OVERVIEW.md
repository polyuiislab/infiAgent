# 项目延伸 · Project extensions

围绕 InfiAgent 的配置式智能体与运行内核，我们也在开发和使用配套的工程工具与运行平台。

## Cosine IDE

Cosine 面向智能体工程开发：内嵌助手理解工程说明，编辑配置、提示词和工具，管理知识与文件，在真实运行环境中试运行，并查看会话过程、输出文件和本地版本记录。

开发者可以把同一工程里的配置、运行镜像和测试样例反复用于验证，减少环境与材料的重复准备。

## 可复用的测试与修复工作流

助手可以运行实际任务，读取动作记录与产物，定位首先出现问题的步骤，修改配置或工具后再跑相同样例，对照前后结果。测试、问题记录和交付物可以随工程持续积累。

```mermaid
flowchart LR
  A[工程配置与测试样例] --> B[真实运行]
  B --> C[查看过程与产物]
  C --> D[定位问题并修改]
  D --> B
  C --> E[复测记录与交付物]
```

## 运维控制台

集中查看节点和任务状态，管理配置、版本、权限与数据源，观察用量和 token 配额，并组织告警与备份流程。

## 高可用运行平台

支持多节点部署、健康检查和可恢复任务的故障接管。相关机制已用于内部部署和故障演练，持续完善部署、升级与恢复流程。

## 当前进展

| 方向 | 状态 |
|---|---|
| Cosine 工程编辑、助手与容器内试运行 | 已有内部实现与使用 |
| 复用样例的执行、定位与迭代修复 | 已用于工程验证；效果随任务与测试覆盖范围变化 |
| 节点、配置、用量、权限与数据源管理 | 已有内部实现与验证 |
| 多节点运行及任务故障接管 | 已完成内部部署与相应演练 |
| 更广的环境适配与测试覆盖 | 持续完善 |

## English overview

**Cosine IDE** brings assistant-guided editing, project files, knowledge, real runtime execution, artifact inspection, and local version history into the agent development workflow.

**Reusable testing and repair workflows** reuse project configurations, runtime environments, and test cases. An assistant can execute a task, inspect records and outputs, locate a failure, update the relevant configuration or tool, and verify the change in another run.

**The operations console** provides node and task visibility, configuration and version management, permissions, data sources, usage, quotas, alerts, and backup workflows.

**The high-availability runtime platform** supports multiple execution nodes, health checks, and recovery of resumable tasks after node failures. These capabilities have internal implementations and deployment validations, with broader environment and test coverage under active development.
