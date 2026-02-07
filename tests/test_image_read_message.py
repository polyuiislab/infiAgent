#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 image_read 的 message 结构：
1. 读取图片 → 压缩 → base64
2. 构建完整的 messages JSON 并打印
3. 调用 LLM 验证图片是否正确传递
"""

import base64
import json
import io
from pathlib import Path

# ===================== 配置 =====================
IMAGE_PATH = Path(__file__).parent / "截屏2026-02-03 08.44.28.png"
API_KEY = "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"
BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openai/google/gemini-3-flash-preview"
MAX_DIM = 1568
JPEG_QUALITY = 85


def load_and_compress_image(image_path: Path) -> tuple:
    """读取图片并压缩，返回 (data_uri, info_str)"""
    from PIL import Image

    img = Image.open(image_path)
    original_size = img.size
    original_format = img.format

    # 转换色彩模式
    if img.mode in ('RGBA', 'P', 'LA'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    # 缩放
    width, height = img.size
    resized = False
    if width > MAX_DIM or height > MAX_DIM:
        if width > height:
            new_width = MAX_DIM
            new_height = int(height * MAX_DIM / width)
        else:
            new_height = MAX_DIM
            new_width = int(width * MAX_DIM / height)
        img = img.resize((new_width, new_height), Image.LANCZOS)
        resized = True

    # 编码为 JPEG
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    image_data = buffer.getvalue()

    image_base64 = base64.b64encode(image_data).decode('utf-8')
    data_uri = f"data:image/jpeg;base64,{image_base64}"

    final_size = img.size
    size_kb = len(image_data) / 1024
    info = (
        f"原始: {original_size} ({original_format}), "
        f"压缩后: {final_size} (JPEG q={JPEG_QUALITY}), "
        f"大小: {size_kb:.1f}KB, "
        f"base64长度: {len(image_base64)} 字符, "
        f"缩放: {'是' if resized else '否'}"
    )
    return data_uri, info


def build_messages(data_uri: str, query: str) -> list:
    """
    构建模拟 image_read 后的完整 messages 结构（方案二）
    """
    messages = [
        # system prompt（简化版）
        {
            "role": "system",
            "content": "你是一个AI助手，请分析用户提供的图片并回答问题。"
        },
        # 初始 user 消息
        {
            "role": "user",
            "content": "请根据当前任务和上下文，执行下一步操作。"
        },
        # assistant 调用 image_read 工具
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_test_001",
                    "type": "function",
                    "function": {
                        "name": "image_read",
                        "arguments": json.dumps({
                            "image_path": "tests/截屏2026-02-03 08.44.28.png",
                            "query": query
                        }, ensure_ascii=False)
                    }
                }
            ]
        },
        # tool result（方案二：纯文字）
        {
            "role": "tool",
            "tool_call_id": "call_test_001",
            "content": "Image loaded successfully. See below."
        },
        # 跟随的 user 消息（方案二：嵌入图片）
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri}
                },
                {
                    "type": "text",
                    "text": f"上面是 image_read 获取的图片。Agent 的问题是: {query}"
                }
            ]
        }
    ]
    return messages


def print_messages_structure(messages: list):
    """打印 messages 结构（隐藏 base64 数据避免刷屏）"""
    print("\n" + "=" * 70)
    print("📋 Messages JSON 结构（base64 数据已截断显示）")
    print("=" * 70)

    for i, msg in enumerate(messages):
        print(f"\n--- messages[{i}] ---")
        # 深拷贝并截断 base64
        display_msg = json.loads(json.dumps(msg))
        if isinstance(display_msg.get("content"), list):
            for part in display_msg["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if url.startswith("data:"):
                        # 只显示前80字符 + 长度
                        part["image_url"]["url"] = url[:80] + f"...({len(url)} chars total)"
        elif isinstance(display_msg.get("content"), str) and len(display_msg["content"]) > 200:
            display_msg["content"] = display_msg["content"][:200] + "..."

        print(json.dumps(display_msg, indent=2, ensure_ascii=False))


def call_llm(messages: list):
    """使用 litellm 调用 LLM"""
    import litellm
    litellm.set_verbose = False
    litellm.drop_params = True

    print("\n" + "=" * 70)
    print(f"🚀 调用 LLM: {MODEL}")
    print(f"   Base URL: {BASE_URL}")
    print(f"   Messages 数量: {len(messages)}")
    print("=" * 70)

    from litellm import completion

    response = completion(
        model=MODEL,
        messages=messages,
        api_key=API_KEY,
        api_base=BASE_URL,
        temperature=0,
        stream=False
    )

    content = response.choices[0].message.content
    print(f"\n✅ LLM 响应:\n")
    print(content)
    print(f"\n📊 Token 使用: {response.usage}")
    return content


if __name__ == "__main__":
    print("=" * 70)
    print("🧪 测试 image_read 消息结构")
    print("=" * 70)

    # 1. 检查图片文件
    if not IMAGE_PATH.exists():
        print(f"❌ 图片文件不存在: {IMAGE_PATH}")
        exit(1)
    print(f"📷 图片路径: {IMAGE_PATH}")
    print(f"   文件大小: {IMAGE_PATH.stat().st_size / 1024:.1f}KB")

    # 2. 读取并压缩图片
    print("\n📦 处理图片...")
    data_uri, info = load_and_compress_image(IMAGE_PATH)
    print(f"   {info}")

    # 3. 构建 messages
    query = "请描述这张截图中的内容，包括界面元素和文字信息。"
    messages = build_messages(data_uri, query)

    # 4. 打印 messages 结构
    print_messages_structure(messages)

    # 5. 调用 LLM
    print("\n" + "=" * 70)
    print("📡 发送到 LLM...")
    print("=" * 70)
    try:
        result = call_llm(messages)
    except Exception as e:
        print(f"\n❌ LLM 调用失败: {e}")
        import traceback
        traceback.print_exc()
