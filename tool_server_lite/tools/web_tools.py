#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络工具 - 使用 crawl4ai
"""

from pathlib import Path
from typing import Dict, Any
import asyncio
import json
import os
import re
import requests
from urllib.parse import urlencode
from .file_tools import BaseTool, get_abs_path

# Crawl4AI 导入
CRAWL4AI_IMPORT_ERROR = ""
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    CRAWL4AI_AVAILABLE = True
except (ImportError, TypeError, Exception) as exc:
    CRAWL4AI_AVAILABLE = False
    CRAWL4AI_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# DuckDuckGo 导入
try:
    from ddgs import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        DDGS_AVAILABLE = True
    except ImportError:
        DDGS_AVAILABLE = False


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """
    crawl4ai 的返回对象在不同版本/运行环境下可能是:
    - 一个带属性的对象 (result.markdown / result.cleaned_html ...)
    - 或者被序列化/包装成 dict
    这里统一做兼容读取，避免 `getattr(...)=None` 造成误判。
    """
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


class CrawlPageTool(BaseTool):
    """网页爬取工具 - 使用 crawl4ai"""

    @staticmethod
    def _crawl4ai_unavailable_error() -> str:
        detail = CRAWL4AI_IMPORT_ERROR.strip()
        if not detail:
            return "crawl4ai not available"
        lower = detail.lower()
        if "no module named" in lower or "crawl4ai" in lower:
            return f"crawl4ai import failed: {detail}. Install with: pip install crawl4ai"
        return f"crawl4ai import failed: {detail}"

    @staticmethod
    def _normalize_runtime_error_message(error: Exception) -> str:
        message = str(error or "").strip()
        lower = message.lower()
        if "browsertype.launch" in lower and "executable doesn't exist" in lower:
            return (
                message
                + "\nPlaywright Chromium executable is missing in the current runtime. "
                + "For local Python envs run: python -m playwright install chromium. "
                + "For packaged desktop builds, ensure PLAYWRIGHT_BROWSERS_PATH points to the bundled .local-browsers directory."
            )
        return message
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        爬取网页内容
        
        Parameters:
            url (str): 网页URL
            save_path (str, optional): 保存结果的相对路径（.md文件）
            download_images (bool, optional): 是否下载图片，默认False
        """
        try:
            if not CRAWL4AI_AVAILABLE:
                return {
                    "status": "error",
                    "output": "",
                    "error": self._crawl4ai_unavailable_error()
                }
            
            url = parameters.get("url")
            save_path = parameters.get("save_path")
            download_images = parameters.get("download_images", False)
            
            if not url:
                return {
                    "status": "error",
                    "output": "",
                    "error": "url is required"
                }
            
            # 爬取页面
            markdown_text = await self._crawl_page(url)
            
            # 处理图片
            if not download_images:
                # 移除图片标记
                markdown_text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", markdown_text)
            
            # 保存到文件
            if save_path:
                abs_save_path = get_abs_path(task_id, save_path)
                abs_save_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(abs_save_path, 'w', encoding='utf-8') as f:
                    f.write(markdown_text)
                
                output = f"结果保存在 {save_path}"
            else:
                output = markdown_text
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": self._normalize_runtime_error_message(e)
            }
    
    async def _crawl_page(self, url: str) -> str:
        """使用 crawl4ai 爬取页面"""
        browser_conf = BrowserConfig(headless=True, verbose=False)
        run_conf = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
        
        async with AsyncWebCrawler(config=browser_conf) as crawler:
            result = await crawler.arun(url, config=run_conf)
            
            # 先看 crawl4ai 是否明确失败
            success = _get_field(result, "success", None)
            if success is False:
                err = _get_field(result, "error_message", "") or _get_field(result, "error", "") or ""
                raise Exception(f"crawl4ai failed: {err[:300]}")

            markdown_attr = _get_field(result, "markdown", None)
            if markdown_attr is None:
                # 输出可诊断信息，便于定位不同版本的字段差异
                if isinstance(result, dict):
                    keys = list(result.keys())[:60]
                    raise Exception(f"Unable to extract markdown from crawl result (missing field: markdown; dict keys sample={keys})")
                attrs = [a for a in dir(result) if not a.startswith("_")][:60]
                raise Exception(f"Unable to extract markdown from crawl result (missing field: markdown; attrs sample={attrs})")

            try:
                markdown_text = (
                    _get_field(markdown_attr, "raw_markdown", None)
                    or _get_field(markdown_attr, "markdown", None)
                    or str(markdown_attr)
                )
            except Exception:
                markdown_text = str(markdown_attr)

            if not (isinstance(markdown_text, str) and markdown_text.strip()):
                # 不做兜底：确保“原生使用 crawl4ai 的 markdown”才算成功
                mtype = type(markdown_attr).__name__
                raw_len = 0
                md_len = 0
                try:
                    raw_len = len((_get_field(markdown_attr, "raw_markdown", "") or ""))
                except Exception:
                    raw_len = 0
                try:
                    md_len = len((_get_field(markdown_attr, "markdown", "") or ""))
                except Exception:
                    md_len = 0
                raise Exception(f"Unable to extract markdown from crawl result (markdown empty; type={mtype}; raw_markdown_len={raw_len}; markdown_len={md_len})")

            return markdown_text


class GoogleScholarSearchTool(BaseTool):
    """谷歌学术搜索工具 - 使用 crawl4ai"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        谷歌学术搜索
        
        Parameters:
            query (str): 搜索关键词
            year_low (int, optional): 年份下限
            year_high (int, optional): 年份上限
            pages (int, optional): 爬取页数，默认1
            save_path (str, optional): 保存结果的相对路径（.md文件）
        """
        try:
            if not CRAWL4AI_AVAILABLE:
                return {
                    "status": "error",
                    "output": "",
                    "error": CrawlPageTool._crawl4ai_unavailable_error()
                }
            
            query = parameters.get("query")
            year_low = parameters.get("year_low")
            year_high = parameters.get("year_high")
            pages = parameters.get("pages", 1)
            save_path = parameters.get("save_path")
            
            if not query:
                return {
                    "status": "error",
                    "output": "",
                    "error": "query is required"
                }
            
            # 爬取学术搜索结果（Google Scholar 在很多网络环境下会被封/重定向/403）
            # 如果失败，则回退到 DuckDuckGo 的 site:scholar.google.com 搜索（无需代理也更稳）
            try:
                all_content = await self._crawl_scholar(query, year_low, year_high, pages)
            except Exception as crawl_err:
                if DDGS_AVAILABLE:
                    ddg_query = f"site:scholar.google.com {query}"
                    results = DDGS().text(ddg_query, max_results=min(10, pages * 10))
                    lines = [
                        "# Google Scholar (fallback via DuckDuckGo)",
                        "",
                        f"- query: `{query}`",
                        f"- note: direct crawling failed: `{str(crawl_err)[:200]}`",
                        "",
                    ]
                    for i, r in enumerate(results or [], 1):
                        title = (r.get("title") or "").strip()
                        href = (r.get("href") or r.get("link") or "").strip()
                        body = (r.get("body") or "").strip()
                        if href:
                            lines.append(f"{i}. **{title or 'Untitled'}**")
                            lines.append(f"   - {href}")
                            if body:
                                lines.append(f"   - {body}")
                    all_content = "\n".join(lines) + "\n"
                else:
                    raise
            
            # 保存到文件
            if save_path:
                # 生成包含搜索参数的文件名
                from pathlib import Path
                save_path_obj = Path(save_path)
                safe_query = re.sub(r'[^\w\s-]', '', query).strip()
                safe_query = re.sub(r'[-\s]+', '_', safe_query)[:50]
                
                year_suffix = ""
                if year_low or year_high:
                    year_suffix = f"_y{year_low or 'X'}-{year_high or 'X'}"
                
                new_filename = f"{save_path_obj.stem}_{safe_query}{year_suffix}_p{pages}{save_path_obj.suffix}"
                final_save_path = str(save_path_obj.parent / new_filename)
                
                abs_save_path = get_abs_path(task_id, final_save_path)
                abs_save_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(abs_save_path, 'w', encoding='utf-8') as f:
                    f.write(all_content)
                
                output = f"结果保存在 {final_save_path}"
            else:
                output = all_content
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }
    
    async def _crawl_scholar(self, query: str, year_low: int, year_high: int, pages: int) -> str:
        """爬取谷歌学术搜索结果"""
        base_url = "https://scholar.google.com/scholar"
        all_content = []
        
        browser_conf = BrowserConfig(headless=True, verbose=False)
        run_conf = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
        
        async with AsyncWebCrawler(config=browser_conf) as crawler:
            for page in range(pages):
                start = page * 10
                
                params = {
                    "start": str(start),
                    "q": query,
                    "as_sdt": "0,5"
                }
                
                if year_low:
                    params["as_ylo"] = str(year_low)
                if year_high:
                    params["as_yhi"] = str(year_high)
                
                url = f"{base_url}?{urlencode(params)}"
                
                result = await crawler.arun(url, config=run_conf)
                
                markdown_attr = _get_field(result, "markdown", None)
                if markdown_attr:
                    markdown_text = (
                        _get_field(markdown_attr, "raw_markdown", None)
                        or _get_field(markdown_attr, "markdown", None)
                        or str(markdown_attr)
                    )
                    # 移除图片
                    markdown_text = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", markdown_text)
                    all_content.append(f"--- Page {page + 1} ---\n{markdown_text}\n")
        
        return '\n'.join(all_content)


class WebSearchTool(BaseTool):
    """网络搜索工具 - 使用 DuckDuckGo"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        网络搜索（DuckDuckGo）
        
        Parameters:
            query (str): 搜索关键词
            max_results (int, optional): 最大结果数，默认10
            save_path (str, optional): 保存结果的相对路径（.md文件）
        """
        try:
            if not DDGS_AVAILABLE:
                return {
                    "status": "error",
                    "output": "",
                    "error": "ddgs not installed. Run: pip install ddgs"
                }
            
            query = parameters.get("query")
            max_results = parameters.get("max_results", 10)
            save_path = parameters.get("save_path")
            
            if not query:
                return {
                    "status": "error",
                    "output": "",
                    "error": "query is required"
                }
            
            # 使用 DuckDuckGo 搜索
            results = DDGS().text(query, max_results=max_results)
            
            # 格式化为 Markdown
            results_md = []
            results_md.append(f"# Search Results: {query}\n")
            results_md.append(f"Total: {len(results)} results\n")
            
            for i, result in enumerate(results, 1):
                title = result.get('title', 'No title')
                url = result.get('href', '')
                snippet = result.get('body', '')
                
                results_md.append(f"## {i}. {title}\n")
                results_md.append(f"**URL**: {url}\n")
                results_md.append(f"**Snippet**: {snippet}\n")
            
            results_text = '\n'.join(results_md)
            
            # 保存到文件
            if save_path:
                # 生成包含搜索参数的文件名
                from pathlib import Path
                save_path_obj = Path(save_path)
                safe_query = re.sub(r'[^\w\s-]', '', query).strip()
                safe_query = re.sub(r'[-\s]+', '_', safe_query)[:50]  # 限制长度
                
                new_filename = f"{save_path_obj.stem}_{safe_query}_n{max_results}{save_path_obj.suffix}"
                final_save_path = str(save_path_obj.parent / new_filename)
                
                abs_save_path = get_abs_path(task_id, final_save_path)
                abs_save_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(abs_save_path, 'w', encoding='utf-8') as f:
                    f.write(results_text)
                
                output = f"结果保存在 {final_save_path}"
            else:
                output = results_text
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }


class YouWebSearchTool(BaseTool):
    """网络搜索工具（可选）- 使用 You.com；默认免 API key（免费 MCP profile）"""

    DEFAULT_MCP_URL = "https://api.you.com/mcp?profile=free"

    @staticmethod
    def _resolve_mcp_endpoint():
        """
        解析 You.com MCP 端点。

        - 默认使用免费 profile（免 API key）；
        - 设置 YDC_API_KEY / YOUCOM_API_KEY 后切换到带认证的端点；
        - YOUCOM_MCP_URL 可覆盖端点地址。
        """
        api_key = os.environ.get("YOUCOM_API_KEY") or os.environ.get("YDC_API_KEY")
        if os.environ.get("YOUCOM_MCP_URL"):
            return os.environ["YOUCOM_MCP_URL"].strip(), api_key
        if api_key:
            return "https://api.you.com/mcp", api_key
        return YouWebSearchTool.DEFAULT_MCP_URL, None

    @staticmethod
    def _parse_mcp_events(resp):
        """
        解析 MCP Streamable HTTP 响应（兼容纯 JSON 与 SSE 两种格式）。

        SSE 切帧只按 LF/CRLF 拆分，避免 str.splitlines()
        在 U+0085/U+2028 等字符上误拆非 ASCII 结果 JSON。
        """
        content_type = (resp.headers.get("Content-Type") or "").lower()
        body = resp.text
        events = []
        if "text/event-stream" not in content_type:
            try:
                events.append(json.loads(body))
            except (ValueError, TypeError):
                pass
            return events
        for raw_frame in re.split(r"\r\n|\n", body):
            line = raw_frame.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                events.append(json.loads(payload))
            except (ValueError, TypeError):
                continue
        return events

    @classmethod
    def _youcom_search(cls, query, max_results=10, timeout=30):
        """
        通过 You.com MCP 的 you-search 工具执行搜索，返回
        [{title, href, body}, ...]，与 DuckDuckGo 结果同构。
        """
        endpoint, api_key = cls._resolve_mcp_endpoint()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if api_key:
            headers["Authorization"] = "Bearer " + api_key

        def _rpc(method, params=None, session_id=None):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params is not None:
                payload["params"] = params
            req_headers = dict(headers)
            if session_id:
                req_headers["Mcp-Session-Id"] = session_id
            resp = requests.post(
                endpoint, data=json.dumps(payload), headers=req_headers, timeout=timeout
            )
            resp.raise_for_status()
            if session_id is None:
                session_id = resp.headers.get("Mcp-Session-Id")
            results = []
            for event in cls._parse_mcp_events(resp):
                if isinstance(event, dict) and event.get("id") == 1:
                    results.append(event)
            return results, session_id

        # initialize, notifications/initialized, tools/call
        init_params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "infiagent-youcom-search", "version": "1.0.0"},
        }
        responses, session_id = _rpc("initialize", init_params)
        if not responses:
            raise RuntimeError("You.com MCP initialize failed: empty response")
        notify_headers = dict(headers)
        if session_id:
            notify_headers["Mcp-Session-Id"] = session_id
        requests.post(
            endpoint,
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            headers=notify_headers,
            timeout=timeout,
        )

        call_params = {
            "name": "you-search",
            "arguments": {"query": query, "count": int(max_results)},
        }
        responses, _ = _rpc("tools/call", call_params, session_id=session_id)
        if not responses:
            raise RuntimeError("You.com MCP tools/call returned no result")

        result = responses[0].get("result") or {}
        if result.get("isError"):
            raise RuntimeError("You.com MCP you-search error: " + json.dumps(result, ensure_ascii=False))

        # 展开 content 文本，解析 {"results": {"web": [...]}} 结构
        content = result.get("content") or []
        text_parts = [
            c.get("text", "") for c in content if isinstance(c, dict) and "text" in c
        ]
        text = "\n".join(text_parts)
        if not text:
            return []
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return []
        web = (data.get("results") or {}).get("web") or []
        results = []
        for item in web:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if not url:
                continue
            snippets = item.get("snippets") or []
            body = item.get("description") or (snippets[0] if snippets else "")
            results.append({
                "title": item.get("title") or "No title",
                "href": url,
                "body": body or "",
            })
        return results[:max_results]

    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        网络搜索（You.com，可选工具；默认免 API key）

        Parameters:
            query (str): 搜索关键词
            max_results (int, optional): 最大结果数，默认10
            save_path (str, optional): 保存结果的相对路径（.md文件）
        """
        try:
            query = parameters.get("query")
            max_results = parameters.get("max_results", 10)
            save_path = parameters.get("save_path")

            if not query:
                return {
                    "status": "error",
                    "output": "",
                    "error": "query is required"
                }

            results = self._youcom_search(query, max_results=int(max_results))

            # 格式化为 Markdown（与 web_search 保持一致）
            results_md = []
            results_md.append(f"# Search Results: {query}\n")
            results_md.append(f"Total: {len(results)} results\n")

            for i, result in enumerate(results, 1):
                title = result.get('title', 'No title')
                url = result.get('href', '')
                snippet = result.get('body', '')

                results_md.append(f"## {i}. {title}\n")
                results_md.append(f"**URL**: {url}\n")
                results_md.append(f"**Snippet**: {snippet}\n")

            results_text = '\n'.join(results_md)

            # 保存到文件（与 web_search 相同的命名规则）
            if save_path:
                save_path_obj = Path(save_path)
                safe_query = re.sub(r'[^\w\s-]', '', query).strip()
                safe_query = re.sub(r'[-\s]+', '_', safe_query)[:50]

                new_filename = f"{save_path_obj.stem}_{safe_query}_n{max_results}{save_path_obj.suffix}"
                final_save_path = str(save_path_obj.parent / new_filename)

                abs_save_path = get_abs_path(task_id, final_save_path)
                abs_save_path.parent.mkdir(parents=True, exist_ok=True)

                with open(abs_save_path, 'w', encoding='utf-8') as f:
                    f.write(results_text)

                output = f"结果保存在 {final_save_path}"
            else:
                output = results_text

            return {
                "status": "success",
                "output": output,
                "error": ""
            }

        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }


class FileDownloadTool(BaseTool):
    """文件下载工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        从URL下载文件
        
        Parameters:
            url (str): 文件URL
            save_path (str): 保存的相对路径
        """
        try:
            url = parameters.get("url")
            save_path = parameters.get("save_path")
            
            abs_save_path = get_abs_path(task_id, save_path)
            abs_save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 下载文件
            response = requests.get(url, stream=True, timeout=60)
            response.raise_for_status()
            
            # 写入文件
            with open(abs_save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            file_size = abs_save_path.stat().st_size
            size_mb = file_size / (1024 * 1024)
            
            return {
                "status": "success",
                "output": f"Downloaded to {save_path} ({size_mb:.2f} MB)",
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }
