# Parallel Search MCP example

Use InfiAgent's existing MCP loader to search the web and fetch page excerpts with
[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp).
The anonymous endpoint needs no account or API key. Search uses Fast mode; the
free tier has lower rate limits and is intended for exploration and light use.

From the repository root, install into a virtual environment with Python 3.10+:

```bash
python -m venv .venv-parallel
source .venv-parallel/bin/activate
# Windows PowerShell: .venv-parallel\Scripts\Activate.ps1
python -m pip install . -r examples/parallel_search/requirements.txt
```

The example constrains the MCP client to 1.x because the current runtime uses its
`streamablehttp_client` API. It uses Streamable HTTP directly, with no local MCP
server or browser installation required.

```bash
python examples/parallel_search/search.py search "Python asyncio task cancellation"
python examples/parallel_search/search.py fetch https://docs.python.org/3/library/asyncio-task.html \
  --objective "How does task cancellation work?"
```

Both commands print URL-backed excerpts from the server. They execute discovered
MCP tools directly, so no LLM configuration is needed. Each invocation prints a
session ID to stderr. Reuse it for calls in the same conversation by placing
`--session-id ID` before `search` or `fetch`.

`mcp.json` supplies the endpoint, request headers and timeouts. The script loads it
via `MLA_MCP_CONFIG_JSON` for its own process, discovers tools through
`get_mcp_tools`, then calls the selected tool through `call_mcp_tool`. It does not
write saved settings. To use the same server in an existing SDK application,
pass the JSON's `servers` list as the SDK's `mcp_servers` constructor argument;
see the [SDK guide](../../docs/SDK_GUIDE.md).

Discovery or tool errors return a nonzero exit code. If the anonymous service
reports a rate limit, wait for its retry window before trying again.
