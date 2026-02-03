# Agent 事件系统 (Event System)

本文档旨在说明 `infiAgent` 项目中的事件驱动架构，为二次开发和系统集成提供指导。

## 1. 设计哲学

为了将核心的Agent业务逻辑与外部的展示、日志、监控等功能解耦，我们引入了一套事件系统。其核心思想遵循**观察者模式**：

-   **事件生产者 (Producer)**：`AgentExecutor` 在其执行的关键节点，只负责生产并“分发”携带结构化数据的事件，它不关心事件如何被消费。
-   **事件消费者 (Consumer/Handler)**：可以有多个事件处理器，它们“订阅”事件流。每个处理器根据自己的职责，对感兴趣的事件进行处理。例如，`ConsoleLogHandler` 负责将事件格式化后打印到控制台，而 `JsonlStreamHandler` 则负责将事件序列化为JSONL格式，供外部工具使用。

这种设计使得添加新的输出渠道（如HTTP SSE、WebSocket、文件日志等）变得非常简单，只需实现一个新的事件处理器并注册即可，无需修改`AgentExecutor`的任何代码。

## 2. 事件划分标准

我们将事件明确划分为两大类，以区分其用途：

### A. 核心生命周期事件 (Lifecycle Events)

这类事件标志着Agent执行过程中的**关键状态转变**。它们是外部系统（如Web UI后端、自动化测试框架、监控面板）进行程序化消费的主要对象。

-   **特征**:
    -   代表一个明确的、重要的业务步骤（如任务开始、LLM调用、工具执行）。
    -   携带结构化、可预测的数据负载 (payload)。
    -   是构建Agent执行轨迹和进行状态分析的基础。
-   **消费建议**: 外部系统和需要理解Agent行为的模块**应该**监听这些事件。

### B. CLI展示事件 (Display Events)

这类事件的唯一目的是在**命令行界面(CLI)中向用户显示信息**。它们本质上是用户界面(UI)的一部分。

-   **特征**:
    -   携带非结构化或半结构化的字符串消息。
    -   用于提供进度反馈、展示非关键信息、打印警告等。
    -   其内容和格式可能会为了提升CLI用户体验而频繁变动。
-   **消费建议**:
    -   只有负责CLI输出的处理器（如`ConsoleLogHandler`）**应该**处理这类事件。
    -   所有其他程序化系统（Web UI后端、JSONL流等）**应该忽略**这类事件。

---

## 3. 当前事件清单

所有事件均定义在 `core/events.py` 中。

### 核心生命周期事件 (Lifecycle Events)

| 事件类 (Event Class)      | `event_type` 字符串 | 触发时机                                       | 主要数据字段 (`payload`)                                  |
| ------------------------- | ------------------- | ---------------------------------------------- | --------------------------------------------------------- |
| `AgentStartEvent`         | `agent_start`       | Agent的 `run` 方法被调用时。                   | `agent_name`, `task_input`                                |
| `AgentEndEvent`           | `agent_end`         | Agent任务结束（成功、失败或超时）。            | `status` (字符串), `result` (最终产出或错误信息)        |
| `LlmCallStartEvent`       | `llm_call_start`    | 即将向大语言模型（LLM）发起请求时。            | `model` (模型名称), `system_prompt` (完整系统提示词)      |
| `LlmCallEndEvent`         | `llm_call_end`      | 收到LLM的响应后。                              | `llm_output` (模型输出文本), `tool_calls` (工具调用列表)    |
| `ToolCallStartEvent`      | `tool_call_start`   | 即将执行一个工具时。                           | `tool_name`, `arguments` (工具参数)                       |
| `ToolCallEndEvent`        | `tool_call_end`     | 工具执行完毕后。                               | `tool_name`, `status`, `result` (工具返回的结果)          |
| `ThinkingEvent`           | `thinking`          | Agent进行反思、规划或总结时。                  | `agent_name`, `result` (思考/规划的文本内容)              |
| `ErrorEvent`              | `error`             | 发生导致任务中断的严重错误时。                 | `error_display` (格式化后的完整错误信息字符串)          |

### CLI展示事件 (Display Events)

| 事件类 (Event Class) | `event_type` 字符串 | 触发时机                               | 主要数据字段 (`payload`)                                  |
| -------------------- | ------------------- | -------------------------------------- | --------------------------------------------------------- |
| `CliDisplayEvent`    | `cli_display`       | 需要在控制台打印任何非关键的状态信息时。 | `message` (要打印的字符串), `style` (`info`, `warning`等) |

---

## 4. 如何拓展新事件

在为系统添加新功能时，请遵循以下准则来决定是否以及如何添加新事件：

### 步骤1: 判断事件类型

首先问自己：**“这个新事件所代表的信息，除了给CLI用户看之外，是否有可能被其他系统（如Web UI、监控、测试）以编程方式利用？”**

-   **如果答案是“是”**:
    -   这应该是一个**核心生命周期事件**。
    -   在 `core/events.py` 中，创建一个新的 `dataclass` 继承自 `AgentEvent`。
    -   为它定义清晰、结构化的字段。
    -   **示例**: 假设我们要引入一个“上下文压缩”的详细事件，供UI展示压缩细节。可以创建一个 `ContextCompressionEvent`，包含 `original_tokens`, `compressed_tokens`, `summary` 等字段。

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
    -   **必须** 更新 `ConsoleLogHandler` (`core/event_handlers.py`)，添加一个对应的 `_print_...` 方法来在CLI中友好地展示它。
    -   **可选** 更新 `JsonlStreamHandler`，如果你希望这个新事件也通过JSONL流推送到外部工具。

-   **如果只是分发了新的`CliDisplayEvent`**:
    -   **无需任何操作**。现有的 `ConsoleLogHandler` 会自动处理它，而其他处理器会忽略它。这就是该设计的优势所在。

遵循以上准则，可以确保我们的事件系统在不断迭代的过程中，始终保持清晰、解耦和易于维护。
