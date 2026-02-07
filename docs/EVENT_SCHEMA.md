# Agent 事件系统 (Event System) - v2

本文档旨在说明 `infiAgent` 项目中经过规范化重构后的事件驱动架构，为二次开发和系统集成提供指导。

## 1. 设计哲学

为了将核心的Agent业务逻辑与外部的展示、日志、监控等功能解耦，我们引入了一套事件系统。其核心思想遵循**观察者模式**：

-   **事件生产者 (Producer)**：`AgentExecutor` 在其执行的关键节点，只负责生产并“分发”携带结构化数据的事件，它不关心事件如何被消费。
-   **事件消费者 (Consumer/Handler)**：可以有多个事件处理器，它们“订阅”事件流。每个处理器根据自己的职责，对感兴趣的事件进行处理。例如，`ConsoleLogHandler` 负责将事件格式化后打印到控制台，而 `JsonlStreamHandler` 则负责将事件序列化为JSONL格式，供外部工具使用。

这种设计使得添加新的输出渠道（如HTTP SSE、WebSocket、文件日志等）变得非常简单，只需实现一个新的事件处理器并注册即可，无需修改`AgentExecutor`的任何代码。

## 2. 事件命名与划分规范

为了保证事件的清晰性和可扩展性，我们遵循以下规范：

### A. 事件命名 (`event_type`)

所有事件的 `event_type` 字符串都遵循 **`phase.domain.action`** 的三段式命名格式：

-   `phase`: 描述事件发生在Agent生命周期的哪个主要阶段。
    -   `prepare`: 任务执行前（循环开始前）的准备阶段。
    -   `run`: Agent的核心执行循环阶段。
    -   `agent`: 贯穿整个生命周期的Agent自身事件。
    -   `system`: 非业务逻辑的系统级事件，如CLI打印或严重错误。
-   `domain`: 事件所属的业务领域。
    -   `model`: 模型选择。
    -   `history`: 对话历史加载。
    -   `llm`: 大语言模型交互。
    -   `tool`: 工具执行。
    -   `thinking`: Agent的自我反思/规划。
-   `action`: 具体的动作。
    -   `start`, `end`, `fail`, `select`, `load` 等。

**示例**: `prepare.model.select` 表示在“准备阶段”，关于“模型领域”的“选择”事件。

### B. 事件类别

我们将事件明确划分为两大类：

-   **核心生命周期事件**: 代表关键状态转变，携带结构化数据，是程序化消费的主要对象。
-   **系统/展示事件**: 主要用于CLI展示或表示非业务逻辑的系统状态（如错误），通常不被外部业务系统消费。

---

## 3. 当前事件清单

所有事件均定义在 `core/events.py` 中。

### 核心生命周期事件

| `event_type` 字符串         | 事件类 (Event Class)      | 触发时机                                          | 主要数据字段 (`payload`)                                  |
| --------------------------- | ------------------------- | ------------------------------------------------- | --------------------------------------------------------- |
| **Agent Lifecycle**         |                           |                                                   |                                                           |
| `agent.start`               | `AgentStartEvent`         | Agent的 `run` 方法被调用时。                      | `agent_name`, `task_input`                                |
| `agent.end`                 | `AgentEndEvent`           | Agent任务结束（成功、失败或超时）。               | `status` (字符串), `result` (最终产出或错误信息)        |
| **Prepare Phase**           |                           |                                                   |                                                           |
| `prepare.model.select`      | `ModelSelectionEvent`     | 在初始化时选择最终要使用的LLM。                   | `requested_model`, `final_model`, `is_fallback`           |
| `prepare.history.load`      | `HistoryLoadEvent`        | 从存储中成功加载历史记录后。                      | `start_turn`, `action_history_len`, `pending_tool_count`  |
| **Run Phase**               |                           |                                                   |                                                           |
| `run.llm.start`             | `LlmCallStartEvent`       | 即将向LLM发起请求时。                             | `model`, `system_prompt`                                  |
| `run.llm.end`               | `LlmCallEndEvent`         | 收到LLM的响应后。                                 | `llm_output`, `tool_calls` (列表)                         |
| `run.tool.start`            | `ToolCallStartEvent`      | 即将执行一个工具时。                              | `tool_name`, `arguments`                                  |
| `run.tool.end`              | `ToolCallEndEvent`        | 工具执行完毕后。                                  | `tool_name`, `status`, `result`                           |
| `run.thinking.start`        | `ThinkingStartEvent`      | Agent开始思考/规划时。                            | `agent_name`, `is_initial` (是否初次规划), `is_forced` (是否强制) |
| `run.thinking.end`          | `ThinkingEndEvent`        | 思考/规划成功结束。                               | `agent_name`, `result` (思考结果文本)                     |
| `run.thinking.fail`         | `ThinkingFailEvent`       | 思考/规划过程中发生错误。                         | `agent_name`, `error_message`                             |

### 系统与展示事件

| `event_type` 字符串       | 事件类 (Event Class)    | 触发时机                                               | 主要数据字段 (`payload`)                                  |
| ------------------------- | ----------------------- | ------------------------------------------------------ | --------------------------------------------------------- |
| `system.error`            | `ErrorEvent`            | 发生导致任务中断的严重错误时。                         | `error_display` (格式化后的完整错误信息字符串)          |
| `system.cli_display`      | `CliDisplayEvent`       | 需要在控制台打印任何非关键的状态信息时。               | `message` (要打印的字符串), `style` (`info`, `warning`等) |

---

## 4. 如何拓展新事件

在为系统添加新功能时，请遵循以下准则来决定是否以及如何添加新事件：

### 步骤1: 判断事件类型

首先问自己：**“这个新事件所代表的信息，除了给CLI用户看之外，是否有可能被其他系统（如Web UI、监控、测试）以编程方式利用？”**

-   **如果答案是“是”**:
    -   这应该是一个**核心生命周期事件**。
    -   在 `core/events.py` 中，创建一个新的 `dataclass` 继承自 `AgentEvent`。
    -   遵循 `phase.domain.action` 的规范为 `event_type` 命名。
    -   为它定义清晰、结构化的字段。
    -   **示例**: 假设我们要引入一个“上下文压缩”的详细事件，供UI展示压缩细节。可以创建一个 `ContextCompressionEvent`，`event_type` 为 `run.context.compress`，并包含 `original_tokens`, `compressed_tokens`, `summary` 等字段。

-   **如果答案是“否”**:
    -   这应该是一个**CLI展示事件**。
    -   **不要创建新的事件类！** 直接在需要打印信息的地方，分发一个 `CliDisplayEvent` 即可。
    -   **示例**: 如果想在开始压缩前打印一条消息，只需在 `_compress_action_history_if_needed` 方法中调用:
        ```python
        self.event_emitter.dispatch(
            CliDisplayEvent(message="正在准备压缩历史记录...")
        )
        ```

### 步骤2: 更新事件处理器

-   **如果添加了新的核心生命周期事件**:
    -   **必须** 更新 `ConsoleLogHandler` (`core/event_handlers.py`)，添加一个对应的 `_print_...` 方法来在CLI中友好地展示它（方法名将 `.` 替换为 `_`）。
    -   **必须** 考虑是否需要更新 `JsonlStreamHandler`。根据新的实现，`JsonlStreamHandler`会自动序列化所有非系统事件，因此通常无需修改，除非需要特殊处理。

-   **如果只是分发了新的`CliDisplayEvent`**:
    -   **无需任何操作**。现有的 `ConsoleLogHandler` 会自动处理它，而 `JsonlStreamHandler` 会自动忽略它。这就是该设计的优势所在。

遵循以上准则，可以确保我们的事件系统在不断迭代的过程中，始终保持清晰、解耦和易于维护。