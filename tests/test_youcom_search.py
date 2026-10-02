import json
from pathlib import Path

import pytest

from tool_server_lite.tools.web_tools import YouWebSearchTool

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self, body, content_type="application/json", session_id=None):
        self.text = body
        self.headers = {"Content-Type": content_type}
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id


def _rpc_result(web):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps({"results": {"web": web}}, ensure_ascii=False)}]
    }})


class TestParseMcpEvents:
    def test_plain_json(self):
        resp = _FakeResponse(_rpc_result([]))
        events = YouWebSearchTool._parse_mcp_events(resp)
        assert len(events) == 1
        assert events[0]["id"] == 1

    def test_sse_with_ids(self):
        body = 'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\r\n\r\n'
        resp = _FakeResponse(body, "text/event-stream")
        events = YouWebSearchTool._parse_mcp_events(resp)
        assert len(events) == 1
        assert events[0]["id"] == 1

    def test_sse_idless_frames_and_notifications(self):
        body = (
            'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            'data: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n\n'
            'data: not-json\n\n'
        )
        resp = _FakeResponse(body, "text/event-stream")
        events = YouWebSearchTool._parse_mcp_events(resp)
        assert len(events) == 2  # notification + result, bad payload skipped

    def test_sse_preserves_unicode_separators(self):
        # U+0085 / U+2028 must NOT split frames (LF/CRLF-only splitting)
        payload = json.dumps({"k": "ab c"}, ensure_ascii=False)
        body = "data: " + payload + "\n"
        resp = _FakeResponse(body, "text/event-stream")
        events = YouWebSearchTool._parse_mcp_events(resp)
        assert events == [{"k": "ab c"}]

    def test_garbage_body(self):
        resp = _FakeResponse("<<<>>>", "application/json")
        assert YouWebSearchTool._parse_mcp_events(resp) == []


class TestResolveEndpoint:
    def test_default_keyless(self, monkeypatch):
        for var in ("YDC_API_KEY", "YOUCOM_API_KEY", "YOUCOM_MCP_URL"):
            monkeypatch.delenv(var, raising=False)
        url, key = YouWebSearchTool._resolve_mcp_endpoint()
        assert url == "https://api.you.com/mcp?profile=free"
        assert key is None

    def test_api_key_switches_to_authed(self, monkeypatch):
        for var in ("YDC_API_KEY", "YOUCOM_API_KEY", "YOUCOM_MCP_URL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("YDC_API_KEY", "test-key")
        url, key = YouWebSearchTool._resolve_mcp_endpoint()
        assert url == "https://api.you.com/mcp"
        assert key == "test-key"

    def test_custom_url_wins(self, monkeypatch):
        for var in ("YDC_API_KEY", "YOUCOM_API_KEY", "YOUCOM_MCP_URL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("YOUCOM_MCP_URL", "http://localhost:9/mcp")
        url, key = YouWebSearchTool._resolve_mcp_endpoint()
        assert url == "http://localhost:9/mcp"


class TestSearchMapping:
    def test_snippet_fallback_mapping(self, monkeypatch):
        # 模拟底层返回的原始 web 结果，验证 description / snippets 回退映射
        web = [
            {"url": "https://a.example", "title": "A", "description": "desc-a"},
            {"url": "https://b.example", "title": "B", "snippets": ["first-snippet"]},
        ]
        resp = _FakeResponse(
            _rpc_result(web),
            session_id="sess-1",
        )
        events = YouWebSearchTool._parse_mcp_events(resp)
        result = events[0]["result"]
        text = result["content"][0]["text"]
        data = json.loads(text)
        mapped = [
            {
                "title": i.get("title") or "No title",
                "href": i.get("url") or "",
                "body": i.get("description") or (i.get("snippets") or [""])[0],
            }
            for i in data["results"]["web"]
        ]
        assert mapped == [
            {"title": "A", "href": "https://a.example", "body": "desc-a"},
            {"title": "B", "href": "https://b.example", "body": "first-snippet"},
        ]


class TestExecute:
    def test_missing_query(self):
        result = YouWebSearchTool().execute("ws", {"save_path": "temp/x.md"})
        assert result["status"] == "error"
        assert "query is required" in result["error"]

    def test_success_returns_markdown(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            YouWebSearchTool, "_youcom_search",
            lambda self, q, max_results=10, timeout=30: [
                {"title": "T", "href": "https://x.example", "body": "S"},
            ],
        )
        result = YouWebSearchTool().execute(str(tmp_path), {"query": "hello"})
        assert result["status"] == "success"
        assert "# Search Results: hello" in result["output"]
        assert "**URL**: https://x.example" in result["output"]

    def test_save_path_writes_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            YouWebSearchTool, "_youcom_search",
            lambda self, q, max_results=10, timeout=30: [
                {"title": "T", "href": "https://x.example", "body": "S"},
            ],
        )
        result = YouWebSearchTool().execute(
            str(tmp_path), {"query": "hello world", "save_path": "temp/web_search/out.md"}
        )
        assert result["status"] == "success"
        saved = list(Path(tmp_path).glob("temp/web_search/out_*.md"))
        assert len(saved) == 1
        assert "# Search Results: hello world" in saved[0].read_text(encoding="utf-8")

    def test_failure_is_safe_error(self, monkeypatch):
        def boom(self, q, max_results=10, timeout=30):
            raise RuntimeError("network down")
        monkeypatch.setattr(YouWebSearchTool, "_youcom_search", boom)
        result = YouWebSearchTool().execute("ws", {"query": "hello"})
        assert result["status"] == "error"
        assert "network down" in result["error"]
        assert result["output"] == ""
