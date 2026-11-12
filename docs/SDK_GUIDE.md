# InfiAgent SDK

## Configure and run

```bash
pip install infiagent==3.13.0
infiagent-runtime --user-root ./user_root
```

Configure models in the developer console, then use the same root in Python:

```python
from infiagent import infiagent

agent = infiagent(user_data_root="./user_root", default_agent_system="OpenCowork")
result = agent.run(
    "Review the project and write a report.",
    task_id="./user_root/tasks/review",
    reasoning_mode="react_lite",
    collect_events=True,
    on_event=lambda event: print(event["event_type"]),
)
print(result["status"], result["output"], result["usage"])
```

Each task requires an explicit `task_id`. `run_async` provides the asynchronous wrapper. SDK environment scopes are serialized within one Python process; parallel children run inside that scope. The HTTP runtime uses separate worker processes for concurrent sessions.

## Agent definitions

An agent system lives in `user_root/agent_library/<system>/`. Define tool schemas in `level_0_tools.yaml` and agents in `level_3_agents.yaml` or other level files. An agent's `available_tools` is enforced at execution time.

The bundled OpenCowork and Researcher systems use the configured role defaults. An agent may override `execution_model`, `thinking_model`, `compressor_model`, `read_figure_model`, or `image_generation_model` explicitly.

## Parallel children

Linked children can be called normally, concurrently in one model response, or through `spawn_subagents`:

```json
{
  "tasks": [
    {"agent": "sub_agent", "task_input": "Inspect implementation", "context_mode": "tool"},
    {"agent": "sub_agent", "task_input": "Inspect tests", "context_mode": "fresh"}
  ],
  "join": "all"
}
```

- `join`: `all`, `any`, `k` (with `k_count`), or `background`.
- `collect`: optional `handles`, `timeout_sec`, and `k`; returns completed results and remaining handles.
- `cancel`: cooperative cancellation of selected children, or all children when handles are omitted.
- `_background: true` on a normal child-agent call returns a handle immediately.
- `collect_subagents` and `cancel_subagents` are aliases.
- `tool` uses normal child context; `fresh` starts from the assigned input without parent task history; `inherit` includes a persisted parent-history seed.

Results are committed before they are acknowledged by the parent. Pending child results must be collected or cancelled and collected before `final_output`. Parent termination cancels outstanding children. Running tools may finish before cancellation reaches a safe point.

## Local workspaces

```python
agent.create_workspace("project", title="Project")
agent.run("Prepare data", task_id="./user_root/tasks/prepare", workspace_id="project")
agent.run("Review data", task_id="./user_root/tasks/review", workspace_id="project")
```

Working files live in `user_root/workspaces/project/files/`. Session state remains under the individual task. A task cannot silently change workspaces.

For concurrent sessions, create a workspace with `concurrency="file_lock"`. File tools use a shared mutex. Other tools must explicitly declare `workspace_compatible: true`; otherwise use the default serial workspace.

## State and recovery

Run the interrupted task again with the same input and `task_id`, or use Resume in the developer console. A committed `final_output` is reused when it already completes that task input. Use `force_new=True` for a new execution generation, or `reset_task` to reset deliberately.

State writes are atomic and write failures propagate. Tool receipts distinguish committed results from calls whose outcome is unknown. An interrupted side-effecting tool is not automatically retried unless it is declared `idempotent: true`; inspect an `OUTCOME_UNKNOWN` result first.

`stopped` and `cancelled` are distinct terminal responses, and do not trigger SDK automatic retry. Model/API errors remain visible in the result or `InfiAgentRunError` when `raise_on_error=True`.

## Human interaction

`human_in_loop` creates a task-local interaction record. Its `occurrence_id` distinguishes repeated questions and concurrent tasks. The developer console renders pending questions and submits answers to that occurrence. CLI integrations can use `get_hil_status` and `respond_hil_task` from `tool_server_lite.tools.human_tools`; pass an occurrence ID when a human-readable `hil_id` is ambiguous.

## Tools and model usage

Custom tools live in `tools_library/`. The default external execution mode uses the current Python interpreter. Built-in tools run through the same execution boundary and argument validation. Declare `parallel_safe: true` only for tools that can safely overlap.

Large tool outputs retain their full content in `.tool_outputs/`; the model receives a preview and reference. `MLA_TOOL_OUTPUT_MAX_CHARS` controls the preview limit. Repeated identical tool errors terminate after the configured `MLA_MAX_CONSECUTIVE_TOOL_ERRORS` limit.

`result.usage` reports the run's input, output, cached and total tokens, including children. The local usage panel aggregates the same per-call records. See [usage statistics](USAGE.md) and [event schema](EVENT_SCHEMA.md).
