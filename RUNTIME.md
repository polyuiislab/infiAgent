# InfiAgent

InfiAgent is a long-horizon multi-agent framework built around three ideas:
**configuration-defined agents** (an agent is YAML, not a class), a **forgetting
loop** (short action windows + periodic thinking summaries + file relay), and a
**resumable execution stack** (all conversation/stack/action state is persisted
under a portable `user_root/`, so tasks survive interruption and resume across
processes and machines).

For free web search and page fetching through the existing MCP runtime, see the
[Parallel Search MCP example](examples/parallel_search/README.md).

```bash
pip install infiagent
```

This package ships the complete backend runtime: the agent execution loop,
context builder, tool executor, the built-in level-0 tool suite, the LLM
client with multi-provider deployment failover, the concurrency-safe
experience store, and the SDK entry point, together with the full test suite.

## Quick start

```python
from infiagent import infiagent

agent = infiagent(
    user_data_root="./user_root",
    agent_library_dir="./user_root",
    default_agent_system="my_system",
    default_agent_name="alpha_agent",
    direct_tools=True,
)
result = agent.run("your task", task_id="./user_root/tasks/demo")
```

Agent systems are defined under `user_root/agent_library/<system>/`
(`level_3_agents.yaml`, `level_0_tools.yaml`, `general_prompts.yaml`);
model access is configured in `user_root/config/llm_config.yaml`
(single key, or a multi-provider `deployments` list with sticky-primary
failover). Task state persists under `user_root/tasks/<task_id>/` and
`user_root/conversations/`, and interrupted tasks can be resumed.

## Reliability characteristics

- **Crash-safe persistence:** runtime state is written atomically
  (same-directory temp file + fsync + rename); corrupted legacy state files
  are quarantined automatically with a clean recovery path, and interrupted
  tool calls replay idempotently on resume.
- **Concurrency-safe experience store:** cross-process file locks over the
  full read-modify-write, uuid entry ids, file revisions with optimistic
  concurrency checks, transactional dual-scope writes, and only
  `status=active` entries are injected into agent context.
- **Multi-provider LLM failover:** a model may declare multiple deployments
  (keys/platforms); healthy traffic sticks to the primary to preserve
  provider-side prompt caching, failures cool down and fail over
  automatically.

## License

GPL-3.0. See `LICENSE`.
