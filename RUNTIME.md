# InfiAgent Runtime

InfiAgent runs configuration-defined agents through a resumable execution loop. The Python SDK, CLI, HTTP API and developer console use the same runtime.

## Run

```bash
pip install infiagent==3.13.0
infiagent-runtime --user-root ./user_root
```

Open http://localhost:4260 and configure the five model roles through the sidebar. The default HTTP bind address is 127.0.0.1.

## Local state

- `config/`: model and runtime settings.
- `agent_library/`, `tools_library/`, `skills/`: reusable agent definitions and tools.
- `tasks/`: session files, tool receipts, interactions and activity records.
- `workspaces/`: shared project files.
- `conversations/`: saved agent action histories.
- `runtime/usage.sqlite`: local usage projection; per-call records also live in each task's `.sdk_runtime/usage.ndjson`.

A failed state write is surfaced to the caller. Unknown outcomes of interrupted side-effecting tools require inspection. Cancellation of running child agents is cooperative: an in-flight tool may finish before the child reaches a safe point.

See [SDK](docs/SDK_GUIDE.md), [Docker](docs/DOCKER_GUIDE.md), and [events](docs/EVENT_SCHEMA.md) for the public interfaces.
