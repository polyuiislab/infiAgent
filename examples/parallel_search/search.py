"""Call Parallel's anonymous search/fetch tools through InfiAgent's MCP loader."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from utils.mcp_manager import call_mcp_tool, get_mcp_failures, get_mcp_tools


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session-id", default=None,
        help="Reuse this ID for related search and fetch calls in one conversation.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Search the web and return excerpts.")
    search.add_argument("query", help="A keyword query, ideally 3-6 words.")
    search.add_argument("--objective", help="The information you want to find.")
    fetch = commands.add_parser("fetch", help="Fetch excerpts from a web page.")
    fetch.add_argument("url")
    fetch.add_argument("--objective", help="Information to extract (up to 200 characters).")
    args = parser.parse_args(argv)

    # Use only this example's anonymous server in the current process. Saved
    # app_config.json and the normal runtime's provider choices are untouched.
    settings = Path(__file__).with_name("mcp.json").read_text(encoding="utf-8")
    os.environ["MLA_MCP_CONFIG_JSON"] = settings
    session_id = args.session_id or str(uuid.uuid4())
    print(f"Session ID: {session_id}", file=sys.stderr)
    tools = get_mcp_tools(force_reload=True)
    tool_name = f"mcp__parallel__web_{args.command}"
    if tool_name not in tools:
        print(json.dumps(get_mcp_failures(), ensure_ascii=False), file=sys.stderr)
        print(f"MCP tool unavailable: {tool_name}", file=sys.stderr)
        return 1

    arguments = {"session_id": session_id}
    if args.command == "search":
        arguments.update(objective=args.objective or args.query, search_queries=[args.query])
    else:
        arguments["urls"] = [args.url]
        if args.objective:
            arguments["objective"] = args.objective
    try:
        result = call_mcp_tool(tools[tool_name], arguments)
    except Exception as exc:
        print(f"MCP call failed: {exc}", file=sys.stderr)
        return 1
    if result["status"] != "success":
        print(result["error"] or result["output"], file=sys.stderr)
        return 1
    print(result["output"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
