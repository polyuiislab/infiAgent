# Runtime events

SDK callbacks receive:

```json
{"event_type":"run.tool.end","phase":"run","domain":"tool","action":"end","payload":{"agent_id":"...","tool_call_id":"...","tool_name":"file_read","status":"success","result":{}}}
```

Execution events include `agent_id` and, for tool calls, `tool_call_id`. Parallel same-named agents have different instance IDs. Agent preparation events may precede assignment of the run's instance ID.

| SDK event | Meaning |
|---|---|
| `agent.start` / `agent.end` | Agent instance lifecycle |
| `run.llm.start` / `run.llm.end` | Model call and returned usage |
| `run.llm.token` | Streamed model content or reasoning |
| `run.thinking.start` / `run.thinking.end` | Thinking step |
| `run.tool.start` / `run.tool.end` | Tool call lifecycle |
| `system.error` | Execution error |

The developer console consumes normalized SSE from `/api/v1/tasks/message/stream` and `/api/v1/tasks/live-stream`: `agent.started`, `agent.finished`, `tool.started`, `tool.finished`, `assistant.content.delta`, `assistant.content.done`, `thinking.delta`, `message.done`, and `run.finished`.

Frames carry an increasing task event ID and `seq`. Reconnect with `after_seq` or `after_id`. Keepalive comments do not represent model activity. `message.done` carries the final result and run usage; a terminal run may have status `success`, `error`, `stopped`, `cancelled`, or `interrupted`.

A human interaction has its own `occurrence_id`; use that value when answering repeated or simultaneous questions. Session history and streamed events refer to the same task records.
