#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器操作工具 - 基于 Playwright
"""

from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import asyncio
import json
import uuid
import random
import math
from datetime import datetime
from .file_tools import BaseTool, get_abs_path

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# 全局浏览器会话管理
# 格式: {browser_id: {browser, context, pages: {page_id: page}, active_page_id, task_id, created_at, auto_snapshot_task}}
BROWSER_SESSIONS = {}


# ============== 人类行为模拟函数 ==============

def _random_delay(min_ms: int = 50, max_ms: int = 150) -> float:
    """生成随机延迟（秒）"""
    return random.randint(min_ms, max_ms) / 1000.0


def _generate_bezier_curve(start: Tuple[float, float], end: Tuple[float, float], 
                          steps: int = 20) -> List[Tuple[float, float]]:
    """
    生成贝塞尔曲线路径，模拟真实的鼠标移动轨迹
    
    Args:
        start: 起始坐标 (x, y)
        end: 结束坐标 (x, y)
        steps: 路径点数量
    
    Returns:
        路径点列表 [(x1, y1), (x2, y2), ...]
    """
    x0, y0 = start
    x3, y3 = end
    
    # 生成两个控制点（添加随机性使轨迹更自然）
    dx = x3 - x0
    dy = y3 - y0
    distance = math.sqrt(dx**2 + dy**2)
    
    # 控制点偏移（相对于直线路径）
    offset_ratio = random.uniform(0.2, 0.4)
    perpendicular_angle = math.atan2(dy, dx) + math.pi / 2
    
    # 控制点1（靠近起点）
    t1 = 0.33
    x1 = x0 + dx * t1 + math.cos(perpendicular_angle) * distance * offset_ratio * random.choice([-1, 1])
    y1 = y0 + dy * t1 + math.sin(perpendicular_angle) * distance * offset_ratio * random.choice([-1, 1])
    
    # 控制点2（靠近终点）
    t2 = 0.67
    x2 = x0 + dx * t2 + math.cos(perpendicular_angle) * distance * offset_ratio * random.choice([-1, 1])
    y2 = y0 + dy * t2 + math.sin(perpendicular_angle) * distance * offset_ratio * random.choice([-1, 1])
    
    # 生成贝塞尔曲线上的点
    points = []
    for i in range(steps + 1):
        t = i / steps
        # 三次贝塞尔曲线公式
        x = ((1-t)**3 * x0 + 
             3 * (1-t)**2 * t * x1 + 
             3 * (1-t) * t**2 * x2 + 
             t**3 * x3)
        y = ((1-t)**3 * y0 + 
             3 * (1-t)**2 * t * y1 + 
             3 * (1-t) * t**2 * y2 + 
             t**3 * y3)
        points.append((round(x, 2), round(y, 2)))
    
    return points


async def _human_like_mouse_move(page: Page, target_x: float, target_y: float):
    """
    模拟人类鼠标移动到目标位置
    
    Args:
        page: Playwright 页面对象
        target_x: 目标 x 坐标
        target_y: 目标 y 坐标
    """
    # 获取当前鼠标位置（假设从随机起点开始）
    viewport = page.viewport_size
    start_x = random.randint(0, viewport['width'] // 2)
    start_y = random.randint(0, viewport['height'] // 2)
    
    # 生成贝塞尔曲线路径
    path = _generate_bezier_curve((start_x, start_y), (target_x, target_y), steps=random.randint(15, 25))
    
    # 沿路径移动鼠标
    for x, y in path:
        await page.mouse.move(x, y)
        # 随机延迟，模拟人类移动速度
        await asyncio.sleep(random.uniform(0.001, 0.005))
    
    # 到达目标后稍微停顿
    await asyncio.sleep(_random_delay(50, 100))


async def _human_like_click(page: Page, selector: str = None, x: float = None, y: float = None, 
                            button: str = "left", delay_ms: int = None):
    """
    模拟人类点击行为
    
    Args:
        page: Playwright 页面对象
        selector: CSS 选择器（如果提供，则点击元素）
        x, y: 坐标位置（如果提供，则点击坐标）
        button: 鼠标按钮 ("left", "right", "middle")
        delay_ms: 按下和释放之间的延迟（毫秒）
    """
    if delay_ms is None:
        delay_ms = random.randint(50, 150)
    
    if selector:
        # 先移动到元素位置（带随机偏移）
        element = page.locator(selector).first
        box = await element.bounding_box()
        if box:
            # 在元素中心附近随机偏移
            offset_x = random.uniform(-box['width'] * 0.3, box['width'] * 0.3)
            offset_y = random.uniform(-box['height'] * 0.3, box['height'] * 0.3)
            target_x = box['x'] + box['width'] / 2 + offset_x
            target_y = box['y'] + box['height'] / 2 + offset_y
        else:
            raise Exception(f"元素不可见或不存在: {selector}")
    elif x is not None and y is not None:
        target_x, target_y = x, y
    else:
        raise Exception("必须提供 selector 或 (x, y) 坐标")
    
    # 移动鼠标到目标位置
    await _human_like_mouse_move(page, target_x, target_y)
    
    # 模拟按下、延迟、释放
    await page.mouse.down(button=button)
    await asyncio.sleep(delay_ms / 1000.0)
    await page.mouse.up(button=button)
    
    # 点击后随机延迟
    await asyncio.sleep(_random_delay(100, 300))


async def _human_like_type(page: Page, selector: str, text: str, delay_range: Tuple[int, int] = (50, 150)):
    """
    模拟人类输入文本（逐字符输入，带随机延迟）
    
    Args:
        page: Playwright 页面对象
        selector: 输入框选择器
        text: 要输入的文本
        delay_range: 每个字符之间的延迟范围（毫秒）
    """
    # 先点击输入框
    await _human_like_click(page, selector=selector)
    
    # 逐字符输入
    for char in text:
        await page.keyboard.type(char)
        # 随机延迟
        delay = random.randint(*delay_range)
        await asyncio.sleep(delay / 1000.0)
        
        # 偶尔有更长的停顿（模拟思考）
        if random.random() < 0.1:  # 10% 概率
            await asyncio.sleep(random.uniform(0.3, 0.8))


async def _auto_snapshot_loop(browser_id: str, task_id: str, interval_seconds: int):
    """定期快照循环"""
    print(f"[INFO] 启动自动快照循环: 每 {interval_seconds} 秒")
    
    while browser_id in BROWSER_SESSIONS:
        try:
            await asyncio.sleep(interval_seconds)
            
            # 检查会话是否还存在
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                break
            
            # 获取活跃页面
            active_page_id = session["active_page_id"]
            page = session["pages"][active_page_id]
            
            # 保存快照
            await _save_page_snapshot(page, browser_id, task_id)
            print(f"[INFO] 自动快照完成 ({browser_id}/{active_page_id})")
            
        except Exception as e:
            print(f"[WARN] 自动快照失败: {e}")
            continue


def _get_browser_dir(task_id: str, browser_id: str) -> Path:
    """获取浏览器会话目录"""
    workspace = Path(task_id)
    browser_dir = workspace / "temp" / "browser" / browser_id
    browser_dir.mkdir(parents=True, exist_ok=True)
    return browser_dir


async def _save_screenshot(page: Page, browser_id: str, task_id: str):
    """保存当前页面截图"""
    browser_dir = _get_browser_dir(task_id, browser_id)
    screenshot_path = browser_dir / "current.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"[INFO] 截图已保存: {screenshot_path}")


async def _save_page_snapshot(page: Page, browser_id: str, task_id: str):
    """保存完整的页面快照（截图 + 内容 + 元素信息）"""
    await _save_screenshot(page, browser_id, task_id)
    await _save_page_content(page, browser_id, task_id)
    await _save_accessibility_tree(page, browser_id, task_id)


async def _save_page_content(page: Page, browser_id: str, task_id: str):
    """保存当前页面内容"""
    browser_dir = _get_browser_dir(task_id, browser_id)
    content_path = browser_dir / "page_content.md"
    
    # 提取页面文本内容
    text_content = await page.evaluate("() => document.body.innerText")
    
    with open(content_path, 'w', encoding='utf-8') as f:
        f.write(f"# {await page.title()}\n\n")
        f.write(f"URL: {page.url}\n\n")
        f.write(f"---\n\n")
        f.write(text_content)
    
    print(f"[INFO] 页面内容已保存: {content_path}")


async def _save_accessibility_tree(page: Page, browser_id: str, task_id: str):
    """保存可访问性树（包含可交互元素信息）"""
    browser_dir = _get_browser_dir(task_id, browser_id)
    elements_path = browser_dir / "current_elements.json"
    
    try:
        # 方案1：使用 Playwright 的 Accessibility Snapshot
        try:
            snapshot = await page.accessibility.snapshot()
            if snapshot:
                # 扁平化 accessibility tree，提取可交互元素
                interactive_elements = _flatten_accessibility_tree(snapshot)
            else:
                interactive_elements = []
        except Exception as e:
            print(f"[WARN] Accessibility snapshot 失败，使用备用方案: {e}")
            interactive_elements = []
        
        # 方案2（备用）：使用 JavaScript 提取常见交互元素
        js_elements = await page.evaluate("""
            () => {
                const elements = [];
                let counter = 0;
                
                // 辅助函数：生成选择器
                const getSelector = (el) => {
                    if (el.id) return `#${el.id}`;
                    if (el.name) return `[name="${el.name}"]`;
                    
                    // 尝试生成简单的选择器
                    let selector = el.tagName.toLowerCase();
                    if (el.className) {
                        const classes = el.className.split(' ').filter(c => c && !c.includes(' '));
                        if (classes.length > 0) {
                            selector += '.' + classes.slice(0, 2).join('.');
                        }
                    }
                    return selector;
                };
                
                // 提取输入框
                document.querySelectorAll('input:not([type="hidden"]), textarea').forEach(el => {
                    if (counter++ > 200) return;  // 限制数量
                    elements.push({
                        type: 'input',
                        input_type: el.type || 'text',
                        role: el.getAttribute('role') || 'textbox',
                        selector: getSelector(el),
                        id: el.id || '',
                        name: el.name || '',
                        placeholder: el.placeholder || '',
                        value: el.value || '',
                        aria_label: el.getAttribute('aria-label') || '',
                        label_text: (() => {
                            const label = el.labels?.[0] || document.querySelector(`label[for="${el.id}"]`);
                            return label ? label.innerText.trim() : '';
                        })()
                    });
                });
                
                // 提取按钮
                document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]').forEach(el => {
                    if (counter++ > 200) return;
                    elements.push({
                        type: 'button',
                        role: 'button',
                        selector: getSelector(el),
                        id: el.id || '',
                        text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().substring(0, 100),
                        aria_label: el.getAttribute('aria-label') || ''
                    });
                });
                
                // 提取链接（限制数量）
                const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 100);
                links.forEach(el => {
                    if (counter++ > 200) return;
                    const text = el.innerText.trim();
                    if (text) {  // 只保留有文字的链接
                        elements.push({
                            type: 'link',
                            role: 'link',
                            selector: getSelector(el),
                            id: el.id || '',
                            text: text.substring(0, 100),
                            href: el.href
                        });
                    }
                });
                
                // 提取下拉框
                document.querySelectorAll('select').forEach(el => {
                    if (counter++ > 200) return;
                    const options = Array.from(el.options).map(opt => ({
                        value: opt.value,
                        text: opt.text
                    }));
                    elements.push({
                        type: 'select',
                        role: 'combobox',
                        selector: getSelector(el),
                        id: el.id || '',
                        name: el.name || '',
                        options: options.slice(0, 20)  // 限制选项数量
                    });
                });
                
                // 提取复选框和单选框
                document.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach(el => {
                    if (counter++ > 200) return;
                    elements.push({
                        type: el.type,
                        role: el.type === 'checkbox' ? 'checkbox' : 'radio',
                        selector: getSelector(el),
                        id: el.id || '',
                        name: el.name || '',
                        checked: el.checked,
                        value: el.value || '',
                        label_text: (() => {
                            const label = el.labels?.[0] || document.querySelector(`label[for="${el.id}"]`);
                            return label ? label.innerText.trim() : '';
                        })()
                    });
                });
                
                return elements;
            }
        """)
        
        # 合并两种方案的结果
        all_elements = interactive_elements + js_elements if interactive_elements else js_elements
        
        data = {
            "url": page.url,
            "title": await page.title(),
            "timestamp": datetime.now().isoformat(),
            "interactive_elements": all_elements,
            "total_elements": len(all_elements),
            "note": "此列表包含页面可见的主要交互元素。对于复杂页面（iframe、动态加载），建议 Agent 结合 Vision 分析和 JavaScript 探测。"
        }
        
        with open(elements_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"[INFO] 可交互元素已保存: {elements_path} (共 {len(all_elements)} 个)")
        
    except Exception as e:
        print(f"[WARN] 保存元素信息失败: {e}")


def _flatten_accessibility_tree(node: dict, elements: list = None) -> list:
    """扁平化 accessibility tree，提取可交互元素"""
    if elements is None:
        elements = []
    
    if not node:
        return elements
    
    # 提取当前节点（如果是可交互元素）
    role = node.get('role', '')
    if role in ['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'menuitem']:
        element_info = {
            "type": role,
            "role": role,
            "name": node.get('name', ''),
            "value": node.get('value', ''),
            "description": node.get('description', ''),
        }
        elements.append(element_info)
    
    # 递归处理子节点
    children = node.get('children', [])
    for child in children:
        _flatten_accessibility_tree(child, elements)
    
    return elements


class BrowserLaunchTool(BaseTool):
    """启动浏览器会话"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        启动浏览器会话
        
        Parameters:
            headless (bool, optional): 是否无头模式，默认 False（显示浏览器）
            width (int, optional): 窗口宽度，默认 1280
            height (int, optional): 窗口高度，默认 800
            auto_snapshot_interval (int, optional): 自动快照间隔（秒），默认 0（不自动快照）
        
        Returns:
            browser_id: 浏览器会话ID
        """
        try:
            if not PLAYWRIGHT_AVAILABLE:
                return {
                    "status": "error",
                    "output": "",
                    "error": "playwright 未安装。请运行: pip install playwright && playwright install chromium"
                }
            
            headless = parameters.get("headless", False)
            width = parameters.get("width", 1280)
            height = parameters.get("height", 800)
            auto_snapshot_interval = parameters.get("auto_snapshot_interval", 0)
            
            # 生成唯一的 browser_id
            browser_id = f"browser_{uuid.uuid4().hex[:8]}"
            
            # 创建浏览器目录
            browser_dir = _get_browser_dir(task_id, browser_id)
            
            # 反检测启动参数
            launch_args = [
                '--disable-blink-features=AutomationControlled',  # 禁用自动化控制特征
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-web-security',  # 可选：禁用某些安全检查
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--disable-gpu',
            ]
            
            if not headless:
                launch_args.append('--start-maximized')
            
            # 启动浏览器
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=headless,
                args=launch_args,
                # 隐藏自动化特征
                chromium_sandbox=False,
            )
            
            # 真实的浏览器指纹
            user_agents = [
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            ]
            
            # 创建上下文（带反检测参数）
            context = await browser.new_context(
                viewport={'width': width, 'height': height},
                user_agent=random.choice(user_agents),
                locale='zh-CN',
                timezone_id='Asia/Shanghai',
                permissions=['geolocation', 'notifications'],
                # 添加真实的浏览器特征
                has_touch=False,
                is_mobile=False,
                device_scale_factor=1,
            )
            
            # 创建第一个页面
            page = await context.new_page()
            
            # 注入反检测脚本（隐藏 webdriver 标志）
            await page.add_init_script("""
                // 覆盖 navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                // 覆盖 Chrome 对象
                window.chrome = {
                    runtime: {}
                };
                
                // 覆盖 permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
                
                // 覆盖 plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                
                // 覆盖 languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en']
                });
            """)
            
            # 注册到全局管理
            BROWSER_SESSIONS[browser_id] = {
                "playwright": playwright,
                "browser": browser,
                "context": context,
                "pages": {"page_0": page},
                "active_page_id": "page_0",
                "task_id": task_id,
                "created_at": datetime.now().isoformat(),
                "auto_snapshot_task": None
            }
            
            # 启动自动快照任务（如果配置了）
            # if auto_snapshot_interval > 0:
            #     snapshot_task = asyncio.create_task(
            #         _auto_snapshot_loop(browser_id, task_id, auto_snapshot_interval)
            #     )
            #     BROWSER_SESSIONS[browser_id]["auto_snapshot_task"] = snapshot_task
            #     print(f"[INFO] 自动快照已启用: 每 {auto_snapshot_interval} 秒")
            
            # 保存元数据
            metadata = {
                "browser_id": browser_id,
                "created_at": datetime.now().isoformat(),
                "headless": headless,
                "viewport": {"width": width, "height": height}
            }
            
            with open(browser_dir / "metadata.json", 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            
            print(f"[INFO] 浏览器已启动: {browser_id}")
            print(f"[INFO] 无头模式: {headless}")
            print(f"[INFO] 窗口尺寸: {width}x{height}")
            
            return {
                "status": "success",
                "output": f"浏览器已启动\n- Browser ID: {browser_id}\n- 初始页面: page_0\n- 截图目录: temp/browser/{browser_id}/",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"启动浏览器失败: {str(e)}"
            }


class BrowserListSessionsTool(BaseTool):
    """列出所有浏览器会话"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        列出当前所有活跃的浏览器会话
        
        Parameters:
            task_id_filter (str, optional): 只列出指定 task_id 的浏览器，不指定则列出所有
        """
        try:
            task_id_filter = parameters.get("task_id_filter")
            
            sessions_info = []
            for browser_id, session in BROWSER_SESSIONS.items():
                # 过滤 task_id
                if task_id_filter and session["task_id"] != task_id_filter:
                    continue
                
                info = {
                    "browser_id": browser_id,
                    "task_id": session["task_id"],
                    "created_at": session["created_at"],
                    "pages_count": len(session["pages"]),
                    "active_page": session["active_page_id"],
                    "auto_snapshot_enabled": session.get("auto_snapshot_task") is not None
                }
                
                # 获取活跃页面的 URL 和标题
                active_page = session["pages"][session["active_page_id"]]
                info["current_url"] = active_page.url
                info["current_title"] = await active_page.title()
                
                sessions_info.append(info)
            
            if not sessions_info:
                return {
                    "status": "success",
                    "output": "当前没有活跃的浏览器会话",
                    "error": ""
                }
            
            # 格式化输出
            output_lines = [f"活跃的浏览器会话（共 {len(sessions_info)} 个）：\n"]
            for info in sessions_info:
                output_lines.append(f"🌐 {info['browser_id']}")
                output_lines.append(f"   任务: {info['task_id']}")
                output_lines.append(f"   创建时间: {info['created_at']}")
                output_lines.append(f"   标签页数: {info['pages_count']}")
                output_lines.append(f"   活跃页面: {info['active_page']}")
                output_lines.append(f"   当前页: {info['current_title']}")
                output_lines.append(f"   URL: {info['current_url']}")
                output_lines.append(f"   自动快照: {'✅' if info['auto_snapshot_enabled'] else '❌'}\n")
            
            return {
                "status": "success",
                "output": "\n".join(output_lines),
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"列出浏览器会话失败: {str(e)}"
            }


class BrowserCloseTool(BaseTool):
    """关闭浏览器会话"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        关闭浏览器会话
        
        Parameters:
            browser_id (str): 浏览器会话ID
        """
        try:
            browser_id = parameters.get("browser_id")
            
            if not browser_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 取消自动快照任务
            if session.get("auto_snapshot_task"):
                session["auto_snapshot_task"].cancel()
                try:
                    await session["auto_snapshot_task"]
                except asyncio.CancelledError:
                    pass
            
            # 关闭浏览器
            await session["context"].close()
            await session["browser"].close()
            await session["playwright"].stop()
            
            # 从全局管理中移除
            del BROWSER_SESSIONS[browser_id]
            
            print(f"[INFO] 浏览器已关闭: {browser_id}")
            
            return {
                "status": "success",
                "output": f"浏览器 {browser_id} 已关闭",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"关闭浏览器失败: {str(e)}"
            }


class BrowserNewPageTool(BaseTool):
    """新建标签页"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        新建标签页
        
        Parameters:
            browser_id (str): 浏览器会话ID
        """
        try:
            browser_id = parameters.get("browser_id")
            
            if not browser_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 创建新页面
            page = await session["context"].new_page()
            
            # 生成 page_id
            page_count = len(session["pages"])
            page_id = f"page_{page_count}"
            
            # 注册页面
            session["pages"][page_id] = page
            session["active_page_id"] = page_id
            
            print(f"[INFO] 新建标签页: {page_id}")
            
            return {
                "status": "success",
                "output": f"新标签页已创建\n- Page ID: {page_id}\n- 当前总页数: {len(session['pages'])}",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"新建标签页失败: {str(e)}"
            }


class BrowserSwitchPageTool(BaseTool):
    """切换到指定标签页"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        切换到指定标签页
        
        Parameters:
            browser_id (str): 浏览器会话ID
            page_id (str): 页面ID（如 'page_0', 'page_1'）
        """
        try:
            browser_id = parameters.get("browser_id")
            page_id = parameters.get("page_id")
            
            if not browser_id or not page_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id 或 page_id"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            if page_id not in session["pages"]:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"页面不存在: {page_id}。可用页面: {list(session['pages'].keys())}"
                }
            
            # 切换活跃页面
            session["active_page_id"] = page_id
            page = session["pages"][page_id]
            
            # 更新完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            print(f"[INFO] 已切换到标签页: {page_id}")
            
            return {
                "status": "success",
                "output": f"已切换到标签页: {page_id}\n- URL: {page.url}\n- 标题: {await page.title()}",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"切换标签页失败: {str(e)}"
            }


class BrowserClosePageTool(BaseTool):
    """关闭指定标签页"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        关闭指定标签页
        
        Parameters:
            browser_id (str): 浏览器会话ID
            page_id (str): 页面ID（如 'page_0', 'page_1'）
        """
        try:
            browser_id = parameters.get("browser_id")
            page_id = parameters.get("page_id")
            
            if not browser_id or not page_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id 或 page_id"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            if page_id not in session["pages"]:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"页面不存在: {page_id}"
                }
            
            # 不能关闭唯一的页面
            if len(session["pages"]) == 1:
                return {
                    "status": "error",
                    "output": "",
                    "error": "不能关闭唯一的标签页。请使用 browser_close 关闭整个浏览器。"
                }
            
            # 关闭页面
            page = session["pages"][page_id]
            await page.close()
            del session["pages"][page_id]
            
            # 如果关闭的是活跃页面，切换到第一个页面
            if session["active_page_id"] == page_id:
                new_active = list(session["pages"].keys())[0]
                session["active_page_id"] = new_active
                
                # 更新完整快照
                active_page = session["pages"][new_active]
                await _save_page_snapshot(active_page, browser_id, task_id)
            
            print(f"[INFO] 标签页已关闭: {page_id}")
            
            return {
                "status": "success",
                "output": f"标签页 {page_id} 已关闭\n- 剩余页面数: {len(session['pages'])}\n- 当前活跃: {session['active_page_id']}",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"关闭标签页失败: {str(e)}"
            }


class BrowserListPagesTool(BaseTool):
    """列出所有标签页"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        列出所有标签页
        
        Parameters:
            browser_id (str): 浏览器会话ID
        """
        try:
            browser_id = parameters.get("browser_id")
            
            if not browser_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 收集所有页面信息
            pages_info = []
            for page_id, page in session["pages"].items():
                info = {
                    "page_id": page_id,
                    "url": page.url,
                    "title": await page.title(),
                    "is_active": page_id == session["active_page_id"]
                }
                pages_info.append(info)
            
            # 格式化输出
            output_lines = [f"浏览器 {browser_id} 的所有标签页：\n"]
            for info in pages_info:
                active_mark = "🟢" if info["is_active"] else "⚪"
                output_lines.append(f"{active_mark} {info['page_id']}")
                output_lines.append(f"   标题: {info['title']}")
                output_lines.append(f"   URL: {info['url']}\n")
            
            return {
                "status": "success",
                "output": "\n".join(output_lines),
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"列出标签页失败: {str(e)}"
            }


class BrowserNavigateTool(BaseTool):
    """导航到指定 URL"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        导航到指定 URL
        
        Parameters:
            browser_id (str): 浏览器会话ID
            url (str): 目标 URL
            wait_until (str, optional): 等待条件，默认 "load"
                - "load": 等待 load 事件
                - "domcontentloaded": 等待 DOM 加载完成
                - "networkidle": 等待网络空闲
        """
        try:
            browser_id = parameters.get("browser_id")
            url = parameters.get("url")
            wait_until = parameters.get("wait_until", "load")
            
            if not browser_id or not url:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id 或 url"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            active_page_id = session["active_page_id"]
            page = session["pages"][active_page_id]
            
            # 导航
            print(f"[INFO] 导航到: {url}")
            await page.goto(url, wait_until=wait_until, timeout=30000)
            
            # 保存完整快照（截图 + 内容 + 元素）
            await _save_page_snapshot(page, browser_id, task_id)
            
            title = await page.title()
            
            return {
                "status": "success",
                "output": f"导航成功\n- URL: {url}\n- 标题: {title}\n- 活跃页面: {active_page_id}\n- 截图: temp/browser/{browser_id}/current.png",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"导航失败: {str(e)}"
            }


class BrowserSnapshotTool(BaseTool):
    """获取页面快照"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        获取当前页面快照
        
        Parameters:
            browser_id (str): 浏览器会话ID
            include_html (bool, optional): 是否包含 HTML 源码，默认 False
        """
        try:
            browser_id = parameters.get("browser_id")
            include_html = parameters.get("include_html", False)
            
            if not browser_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            active_page_id = session["active_page_id"]
            page = session["pages"][active_page_id]
            
            # 更新完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            # 获取页面信息
            title = await page.title()
            url = page.url
            
            # 提取文本内容
            text_content = await page.evaluate("() => document.body.innerText")
            
            output_lines = [
                f"页面快照（{active_page_id}）",
                f"- 标题: {title}",
                f"- URL: {url}",
                f"- 截图: temp/browser/{browser_id}/current.png",
                f"- 文本内容: temp/browser/{browser_id}/page_content.md",
                ""
            ]
            
            if include_html:
                html_content = await page.content()
                browser_dir = _get_browser_dir(task_id, browser_id)
                html_path = browser_dir / "page_source.html"
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                output_lines.append(f"- HTML 源码: temp/browser/{browser_id}/page_source.html")
            
            return {
                "status": "success",
                "output": "\n".join(output_lines),
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"获取快照失败: {str(e)}"
            }


class BrowserExecuteJsTool(BaseTool):
    """执行 JavaScript 代码"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        在当前页面执行 JavaScript 代码
        
        Parameters:
            browser_id (str): 浏览器会话ID
            script (str): 要执行的 JavaScript 代码
            save_result (bool, optional): 是否保存执行结果到文件，默认 False
        """
        try:
            browser_id = parameters.get("browser_id")
            script = parameters.get("script")
            save_result = parameters.get("save_result", False)
            
            if not browser_id or not script:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id 或 script"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            active_page_id = session["active_page_id"]
            page = session["pages"][active_page_id]
            
            # 执行 JavaScript
            print(f"[INFO] 执行 JavaScript (页面 {active_page_id}):")
            print(f"[INFO] {script[:100]}{'...' if len(script) > 100 else ''}")
            
            result = await page.evaluate(script)
            
            # 等待页面稳定（给时间让 DOM 更新）
            await page.wait_for_timeout(500)
            
            # 保存完整快照（截图 + 内容 + 元素）
            await _save_page_snapshot(page, browser_id, task_id)
            
            # 格式化结果
            result_str = json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "null"
            
            # 保存结果到文件
            if save_result and result is not None:
                browser_dir = _get_browser_dir(task_id, browser_id)
                result_path = browser_dir / "js_result.json"
                with open(result_path, 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                result_info = f"\n- 结果已保存: temp/browser/{browser_id}/js_result.json"
            else:
                result_info = ""
            
            return {
                "status": "success",
                "output": f"JavaScript 执行成功\n- 返回值: {result_str[:500]}{'...' if len(result_str) > 500 else ''}\n- 截图已更新: temp/browser/{browser_id}/current.png{result_info}",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"执行 JavaScript 失败: {str(e)}"
            }


class BrowserClickTool(BaseTool):
    """点击页面元素（封装）"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        点击页面元素
        
        Parameters:
            browser_id (str): 浏览器会话ID
            selector (str): CSS 选择器
            timeout (int, optional): 等待元素出现的超时时间（毫秒），默认 5000
            human_like (bool, optional): 是否使用人类化点击，默认 True
            button (str, optional): 鼠标按钮 ("left", "right", "middle")，默认 "left"
        """
        try:
            browser_id = parameters.get("browser_id")
            selector = parameters.get("selector")
            timeout = parameters.get("timeout", 5000)
            human_like = parameters.get("human_like", True)
            button = parameters.get("button", "left")
            
            if not browser_id or not selector:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id 或 selector"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            # 等待元素出现
            print(f"[INFO] 点击元素: {selector}")
            await page.wait_for_selector(selector, timeout=timeout)
            
            if human_like:
                # 使用人类化点击
                await _human_like_click(page, selector=selector, button=button)
            else:
                # 直接点击
                await page.click(selector)
            
            # 等待页面稳定
            await asyncio.sleep(_random_delay(300, 500))
            
            # 保存完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            return {
                "status": "success",
                "output": f"点击成功: {selector}\n- 点击方式: {'人类化' if human_like else '直接点击'}\n- 截图已更新: temp/browser/{browser_id}/current.png",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"点击失败: {str(e)}"
            }


class BrowserTypeTool(BaseTool):
    """在输入框输入文本（封装）"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        在输入框输入文本
        
        Parameters:
            browser_id (str): 浏览器会话ID
            selector (str): CSS 选择器
            text (str): 要输入的文本
            clear_first (bool, optional): 是否先清空，默认 True
            human_like (bool, optional): 是否使用人类化输入（逐字符），默认 True
            delay_range (tuple, optional): 字符间延迟范围（毫秒），默认 (50, 150)
        """
        try:
            browser_id = parameters.get("browser_id")
            selector = parameters.get("selector")
            text = parameters.get("text")
            clear_first = parameters.get("clear_first", True)
            human_like = parameters.get("human_like", True)
            delay_range = parameters.get("delay_range", (50, 150))
            
            if not browser_id or not selector or text is None:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id, selector 或 text"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            # 输入文本
            print(f"[INFO] 在 {selector} 输入文本")
            
            # 如果需要清空，先清空
            if clear_first:
                await page.fill(selector, "")
            
            if human_like:
                # 使用人类化输入
                await _human_like_type(page, selector, text, delay_range)
            else:
                # 快速输入
                if clear_first:
                    await page.fill(selector, text)
                else:
                    await page.type(selector, text, delay=0)
            
            # 等待页面稳定
            await asyncio.sleep(_random_delay(300, 500))
            
            # 保存完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            return {
                "status": "success",
                "output": f"文本输入成功: {selector}\n- 输入方式: {'人类化（逐字符）' if human_like else '直接填充'}\n- 截图已更新: temp/browser/{browser_id}/current.png",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"输入文本失败: {str(e)}"
            }


class BrowserWaitTool(BaseTool):
    """等待条件满足"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        等待条件满足
        
        Parameters:
            browser_id (str): 浏览器会话ID
            wait_type (str): 等待类型
                - "selector": 等待元素出现
                - "navigation": 等待页面导航完成
                - "timeout": 等待指定时间（毫秒）
            selector (str, optional): CSS 选择器（wait_type="selector" 时必需）
            timeout (int, optional): 超时时间（毫秒），默认 30000
            milliseconds (int, optional): 等待时长（wait_type="timeout" 时必需）
        """
        try:
            browser_id = parameters.get("browser_id")
            wait_type = parameters.get("wait_type")
            selector = parameters.get("selector")
            timeout = parameters.get("timeout", 30000)
            milliseconds = parameters.get("milliseconds")
            
            if not browser_id or not wait_type:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id 或 wait_type"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            if wait_type == "selector":
                if not selector:
                    return {
                        "status": "error",
                        "output": "",
                        "error": "wait_type='selector' 时必须提供 selector 参数"
                    }
                print(f"[INFO] 等待元素出现: {selector}")
                await page.wait_for_selector(selector, timeout=timeout)
            
            elif wait_type == "navigation":
                print(f"[INFO] 等待页面导航完成")
                await page.wait_for_load_state("networkidle", timeout=timeout)
            
            elif wait_type == "timeout":
                if not milliseconds:
                    return {
                        "status": "error",
                        "output": "",
                        "error": "wait_type='timeout' 时必须提供 milliseconds 参数"
                    }
                print(f"[INFO] 等待 {milliseconds} 毫秒")
                await page.wait_for_timeout(milliseconds)
            
            else:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"不支持的 wait_type: {wait_type}。可选: selector, navigation, timeout"
                }
            
            # 保存完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            return {
                "status": "success",
                "output": f"等待完成: {wait_type}",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"等待失败: {str(e)}"
            }


class BrowserMouseMoveTool(BaseTool):
    """鼠标移动到指定坐标"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        鼠标移动到指定坐标（模拟人类移动轨迹）
        
        Parameters:
            browser_id (str): 浏览器会话ID
            x (float): 目标 x 坐标
            y (float): 目标 y 坐标
            human_like (bool, optional): 是否使用人类化移动（贝塞尔曲线），默认 True
        """
        try:
            browser_id = parameters.get("browser_id")
            x = parameters.get("x")
            y = parameters.get("y")
            human_like = parameters.get("human_like", True)
            
            if not browser_id or x is None or y is None:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id, x 或 y"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            print(f"[INFO] 移动鼠标到: ({x}, {y})")
            
            if human_like:
                # 使用人类化移动
                await _human_like_mouse_move(page, x, y)
            else:
                # 直接移动
                await page.mouse.move(x, y)
            
            return {
                "status": "success",
                "output": f"鼠标已移动到坐标: ({x}, {y})\n- 移动方式: {'人类化轨迹' if human_like else '直接移动'}",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"鼠标移动失败: {str(e)}"
            }


class BrowserMouseClickCoordsTool(BaseTool):
    """在指定坐标位置点击"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        在指定坐标位置点击
        
        Parameters:
            browser_id (str): 浏览器会话ID
            x (float): 点击 x 坐标
            y (float): 点击 y 坐标
            button (str, optional): 鼠标按钮 ("left", "right", "middle")，默认 "left"
            click_count (int, optional): 点击次数（双击用2），默认 1
            human_like (bool, optional): 是否使用人类化点击，默认 True
        """
        try:
            browser_id = parameters.get("browser_id")
            x = parameters.get("x")
            y = parameters.get("y")
            button = parameters.get("button", "left")
            click_count = parameters.get("click_count", 1)
            human_like = parameters.get("human_like", True)
            
            if not browser_id or x is None or y is None:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id, x 或 y"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            print(f"[INFO] 在坐标 ({x}, {y}) 点击 {click_count} 次")
            
            if human_like:
                # 使用人类化点击
                for _ in range(click_count):
                    await _human_like_click(page, x=x, y=y, button=button)
                    if click_count > 1:
                        await asyncio.sleep(_random_delay(50, 150))
            else:
                # 直接点击
                await page.mouse.click(x, y, button=button, click_count=click_count)
            
            # 等待页面稳定
            await asyncio.sleep(_random_delay(300, 500))
            
            # 保存完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            return {
                "status": "success",
                "output": f"坐标点击成功: ({x}, {y})\n- 按钮: {button}\n- 点击次数: {click_count}\n- 截图已更新: temp/browser/{browser_id}/current.png",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"坐标点击失败: {str(e)}"
            }


class BrowserDragAndDropTool(BaseTool):
    """鼠标拖拽操作"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        鼠标拖拽操作（从起点拖到终点）
        
        Parameters:
            browser_id (str): 浏览器会话ID
            from_x (float): 起始 x 坐标
            from_y (float): 起始 y 坐标
            to_x (float): 目标 x 坐标
            to_y (float): 目标 y 坐标
            human_like (bool, optional): 是否使用人类化拖拽，默认 True
        """
        try:
            browser_id = parameters.get("browser_id")
            from_x = parameters.get("from_x")
            from_y = parameters.get("from_y")
            to_x = parameters.get("to_x")
            to_y = parameters.get("to_y")
            human_like = parameters.get("human_like", True)
            
            if not browser_id or from_x is None or from_y is None or to_x is None or to_y is None:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id, from_x, from_y, to_x 或 to_y"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            print(f"[INFO] 拖拽: ({from_x}, {from_y}) -> ({to_x}, {to_y})")
            
            if human_like:
                # 人类化拖拽
                # 1. 移动到起点
                await _human_like_mouse_move(page, from_x, from_y)
                await asyncio.sleep(_random_delay(100, 200))
                
                # 2. 按下鼠标
                await page.mouse.down()
                await asyncio.sleep(_random_delay(50, 100))
                
                # 3. 生成拖拽路径
                path = _generate_bezier_curve((from_x, from_y), (to_x, to_y), steps=random.randint(20, 30))
                
                # 4. 沿路径移动
                for x, y in path[1:]:  # 跳过第一个点（起点）
                    await page.mouse.move(x, y)
                    await asyncio.sleep(random.uniform(0.002, 0.008))
                
                # 5. 释放鼠标
                await asyncio.sleep(_random_delay(50, 100))
                await page.mouse.up()
            else:
                # 直接拖拽
                await page.mouse.move(from_x, from_y)
                await page.mouse.down()
                await page.mouse.move(to_x, to_y)
                await page.mouse.up()
            
            # 等待页面稳定
            await asyncio.sleep(_random_delay(300, 500))
            
            # 保存完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            return {
                "status": "success",
                "output": f"拖拽完成: ({from_x}, {from_y}) -> ({to_x}, {to_y})\n- 截图已更新: temp/browser/{browser_id}/current.png",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"拖拽失败: {str(e)}"
            }


class BrowserHoverTool(BaseTool):
    """鼠标悬停操作"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        鼠标悬停在元素或坐标上
        
        Parameters:
            browser_id (str): 浏览器会话ID
            selector (str, optional): CSS 选择器（悬停在元素上）
            x (float, optional): x 坐标（悬停在坐标上）
            y (float, optional): y 坐标（悬停在坐标上）
            duration_ms (int, optional): 悬停持续时间（毫秒），默认 1000
            human_like (bool, optional): 是否使用人类化移动，默认 True
        
        注意：selector 和 (x, y) 必须提供其中一个
        """
        try:
            browser_id = parameters.get("browser_id")
            selector = parameters.get("selector")
            x = parameters.get("x")
            y = parameters.get("y")
            duration_ms = parameters.get("duration_ms", 1000)
            human_like = parameters.get("human_like", True)
            
            if not browser_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id"
                }
            
            if not selector and (x is None or y is None):
                return {
                    "status": "error",
                    "output": "",
                    "error": "必须提供 selector 或 (x, y) 坐标"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            # 确定目标坐标
            if selector:
                print(f"[INFO] 悬停在元素: {selector}")
                element = page.locator(selector).first
                box = await element.bounding_box()
                if not box:
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"元素不可见或不存在: {selector}"
                    }
                # 元素中心
                target_x = box['x'] + box['width'] / 2
                target_y = box['y'] + box['height'] / 2
            else:
                print(f"[INFO] 悬停在坐标: ({x}, {y})")
                target_x, target_y = x, y
            
            # 移动到目标位置
            if human_like:
                await _human_like_mouse_move(page, target_x, target_y)
            else:
                await page.mouse.move(target_x, target_y)
            
            # 悬停指定时长
            await asyncio.sleep(duration_ms / 1000.0)
            
            # 保存完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            return {
                "status": "success",
                "output": f"悬停完成\n- 位置: {selector if selector else f'({target_x}, {target_y})'}\n- 持续时间: {duration_ms}ms\n- 截图已更新: temp/browser/{browser_id}/current.png",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"悬停失败: {str(e)}"
            }


class BrowserScrollTool(BaseTool):
    """鼠标滚轮滚动操作"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        使用鼠标滚轮滚动页面或元素
        
        Parameters:
            browser_id (str): 浏览器会话ID
            delta_y (int): 垂直滚动距离（像素）。正数向下滚动，负数向上滚动
            delta_x (int, optional): 水平滚动距离（像素），默认 0。正数向右滚动，负数向左滚动
            selector (str, optional): 要滚动的元素 CSS 选择器。不指定则滚动整个页面
            smooth (bool, optional): 是否平滑滚动（分多次小步滚动），默认 True
            human_like (bool, optional): 是否先移动鼠标到元素（人类化），默认 True
        """
        try:
            browser_id = parameters.get("browser_id")
            delta_y = parameters.get("delta_y")
            delta_x = parameters.get("delta_x", 0)
            selector = parameters.get("selector")
            smooth = parameters.get("smooth", True)
            human_like = parameters.get("human_like", True)
            
            if not browser_id or delta_y is None:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: browser_id 或 delta_y"
                }
            
            session = BROWSER_SESSIONS.get(browser_id)
            if not session:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"浏览器会话不存在: {browser_id}"
                }
            
            # 获取活跃页面
            page = session["pages"][session["active_page_id"]]
            
            # 如果指定了元素，先移动鼠标到元素位置
            if selector:
                print(f"[INFO] 滚动元素: {selector}, 距离: ({delta_x}, {delta_y})")
                element = page.locator(selector).first
                box = await element.bounding_box()
                if not box:
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"元素不可见或不存在: {selector}"
                    }
                
                # 移动鼠标到元素中心
                target_x = box['x'] + box['width'] / 2
                target_y = box['y'] + box['height'] / 2
                
                if human_like:
                    await _human_like_mouse_move(page, target_x, target_y)
                else:
                    await page.mouse.move(target_x, target_y)
                
                await asyncio.sleep(_random_delay(50, 100))
            else:
                print(f"[INFO] 滚动页面, 距离: ({delta_x}, {delta_y})")
            
            # 执行滚动
            if smooth and abs(delta_y) > 100:
                # 平滑滚动：分多次小步滚动
                steps = min(int(abs(delta_y) / 50), 20)  # 最多20步
                step_y = delta_y / steps
                step_x = delta_x / steps
                
                for i in range(steps):
                    await page.mouse.wheel(step_x, step_y)
                    # 每次滚动后随机延迟，模拟真实滚动
                    await asyncio.sleep(random.uniform(0.02, 0.05))
            else:
                # 直接滚动
                await page.mouse.wheel(delta_x, delta_y)
            
            # 等待页面稳定（滚动可能触发懒加载）
            await asyncio.sleep(_random_delay(300, 500))
            
            # 保存完整快照
            await _save_page_snapshot(page, browser_id, task_id)
            
            return {
                "status": "success",
                "output": f"滚动完成\n- 位置: {selector if selector else '整个页面'}\n- 距离: 垂直 {delta_y}px, 水平 {delta_x}px\n- 模式: {'平滑滚动' if smooth else '直接滚动'}\n- 截图已更新: temp/browser/{browser_id}/current.png",
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"滚动失败: {str(e)}"
            }

