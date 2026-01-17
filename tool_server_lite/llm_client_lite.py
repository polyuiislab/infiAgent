#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量级多模态LLM客户端 - 专供tool_server使用
支持：文本、图片、音频等多模态输入
"""

import os
import yaml
import base64
from pathlib import Path
from typing import Optional
from litellm import completion
import litellm

# 尝试导入 transcribe，如果不支持则使用替代方案
try:
    from litellm import transcribe
    HAS_TRANSCRIBE = True
except ImportError:
    HAS_TRANSCRIBE = False
    # 如果没有transcribe，需要使用openai直接调用
    try:
        import openai
        HAS_OPENAI = True
    except ImportError:
        HAS_OPENAI = False


class LLMClientLite:
    """轻量级多模态LLM客户端 - 供tool_server工具使用"""
    
    def __init__(self, llm_config_path: str = None):
        """
        初始化LLM客户端
        
        Args:
            llm_config_path: LLM配置文件路径，默认读取项目配置
        """
        # 加载LLM配置
        if llm_config_path is None:
            # 从tool_server_lite目录找到config
            current_dir = Path(__file__).parent
            config_path = current_dir.parent / "config" / "run_env_config" / "llm_config.yaml"
            
            if not config_path.exists():
                raise FileNotFoundError(f"LLM配置文件不存在: {config_path}")
            
            llm_config_path = str(config_path)
        
        if not os.path.exists(llm_config_path):
            raise FileNotFoundError(f"LLM配置文件不存在: {llm_config_path}")
        
        # 保存配置文件路径（用于后续可能的重载）
        self.config_path = llm_config_path
        
        with open(llm_config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # 读取配置
        self.base_url = self.config.get("base_url", "")
        self.api_key = self.config.get("api_key", "")
        self.models = self.config.get("models", [])
        self.figure_models = self.config.get("figure_models", [])
        self.compressor_models = self.config.get("compressor_models", [])
        self.read_figure_models = self.config.get("read_figure_models", [])
        self.temperature = self.config.get("temperature", 0)
        self.max_tokens = self.config.get("max_tokens", 0)
        
        if not self.api_key:
            raise ValueError("未配置API密钥")
        
        if not self.models:
            raise ValueError("未配置可用模型列表")
        
        # 配置LiteLLM
        litellm.set_verbose = False
        litellm.drop_params = True
        
        print(f"✅ LLM客户端配置已加载: {llm_config_path}")
    
    def reload_config(self):
        """
        重新加载配置文件
        
        用于在运行时更新配置而无需重启服务
        """
        print(f"🔄 重新加载配置文件: {self.config_path}")
        
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # 更新配置
        self.base_url = self.config.get("base_url", "")
        self.api_key = self.config.get("api_key", "")
        self.models = self.config.get("models", [])
        self.figure_models = self.config.get("figure_models", [])
        self.compressor_models = self.config.get("compressor_models", [])
        self.read_figure_models = self.config.get("read_figure_models", [])
        self.temperature = self.config.get("temperature", 0)
        self.max_tokens = self.config.get("max_tokens", 0)
        
        if not self.api_key:
            raise ValueError("未配置API密钥")
        
        if not self.models:
            raise ValueError("未配置可用模型列表")
        
        print(f"✅ 配置已重新加载")
    
    def vision_query(
        self,
        image_path: str,
        question: str = "请描述这张图片的内容",
        model: Optional[str] = None
    ) -> str:
        """
        调用Vision模型分析图片
        
        Args:
            image_path: 图片文件路径（绝对路径）
            question: 要问的问题
            model: 模型名称，默认使用配置中的第一个可用模型
            
        Returns:
            LLM的响应文本
            
        Raises:
            FileNotFoundError: 图片文件不存在
            Exception: LLM调用失败
        """
        # 检查图片文件
        img_path = Path(image_path)
        if not img_path.exists():
            raise FileNotFoundError(f"图片文件不存在: {image_path}")
        
        # 读取并编码图片
        with open(img_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
        
        # 判断图片格式
        suffix = img_path.suffix.lower()
        mime_type_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        mime_type = mime_type_map.get(suffix, 'image/jpeg')
        
        # 构建Vision消息
        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": question
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_data}"
                    }
                }
            ]
        }]
        
        # 选择模型
        if model is None:
            model = self.read_figure_models[0]
        
        # 调用LLM
        try:
            response = completion(
                model=model,
                messages=messages,
                temperature=self.temperature,
                api_key=self.api_key,
                api_base=self.base_url,
                timeout=300  # 5分钟超时保护
            )
            
            # 提取响应
            if response.choices and len(response.choices) > 0:
                return response.choices[0].message.content
            else:
                raise Exception("LLM响应格式异常：缺少choices字段")
                
        except Exception as e:
            raise Exception(f"调用LLM Vision API失败: {str(e)}")

    def create_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        reference_images: Optional[list[str]] = None,
        size: str = "1024x1024",
        n: int = 1,
        response_format: str = "b64_json"
    ) -> str | list[str]:
        """
        调用模型生成图片（支持参考图）
        
        Args:
            prompt: 提示词
            model: 模型名称，默认使用 figure_models 中的第一个
            reference_images: 参考图片路径列表（可选），用于图片编辑/风格迁移
            size: 图片尺寸，默认 "1024x1024"
            n: 生成图片数量，默认 1
            response_format: 返回格式 "b64_json" 或 "url"，默认 "b64_json"
            
        Returns:
            单图时返回一个 base64 数据 URL 或 HTTP URL
            多图时返回 URL 列表
            
        Note:
            - OpenRouter: 使用 chat.completions + modalities (+ 参考图)
            - 其他 API: 使用 litellm.image_generation() (纯生成) 或 litellm.image_edit() (有参考图)
        """
        if model is None:
            if self.figure_models:
                # 兼容字符串或字典格式
                first_model = self.figure_models[0]
                model = first_model if isinstance(first_model, str) else first_model.get("name")
            else:
                model = "dall-e-3"
        
        try:
            has_reference = reference_images and len(reference_images) > 0
            print(f"[INFO] 调用图片生成 API: {model}")
            if has_reference:
                print(f"[INFO] 参考图片数量: {len(reference_images)}")
            if self.base_url:
                print(f"[INFO] 使用自定义端点: {self.base_url}")
            
            # 判断是否是 OpenRouter
            is_openrouter = self.base_url and 'openrouter' in self.base_url.lower()
            
            if is_openrouter:
                # OpenRouter：使用 chat.completions + modalities
                from openai import OpenAI
                
                print(f"[INFO] 使用 OpenRouter 方式")
                
                client = OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                )
                
                # 构建 content
                if has_reference:
                    # 有参考图：构建多模态 content
                    content = [{"type": "text", "text": prompt}]
                    
                    for img_path_str in reference_images:
                        img_path = Path(img_path_str)
                        if not img_path.exists():
                            raise FileNotFoundError(f"参考图片不存在: {img_path_str}")
                        
                        # 读取并编码图片
                        with open(img_path, "rb") as image_file:
                            image_data = base64.b64encode(image_file.read()).decode('utf-8')
                        
                        # 判断图片格式
                        suffix = img_path.suffix.lower()
                        mime_type_map = {
                            '.jpg': 'image/jpeg',
                            '.jpeg': 'image/jpeg',
                            '.png': 'image/png',
                            '.gif': 'image/gif',
                            '.webp': 'image/webp'
                        }
                        mime_type = mime_type_map.get(suffix, 'image/jpeg')
                        
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_data}"
                            }
                        })
                else:
                    # 纯文本生成
                    content = prompt
                
                # 构建 extra_body
                extra_body = {"modalities": ["image", "text"]}
                
                # 添加 image_config（宽高比）
                if size and "x" in size:
                    width, height = map(int, size.split("x"))
                    from math import gcd
                    g = gcd(width, height)
                    ratio_w, ratio_h = width // g, height // g
                    aspect_ratio = f"{ratio_w}:{ratio_h}"
                    if aspect_ratio in ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]:
                        extra_body["image_config"] = {"aspect_ratio": aspect_ratio}
                
                # 调用 API
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                    extra_body=extra_body
                )
                
                # 提取图片
                message = response.choices[0].message
                results = []
                
                if hasattr(message, 'images') and message.images:
                    for image in message.images:
                        if isinstance(image, dict):
                            image_url = image.get('image_url', {}).get('url')
                            if image_url:
                                results.append(image_url)
                        elif hasattr(image, 'image_url'):
                            url = getattr(image.image_url, 'url', None)
                            if url:
                                results.append(url)
                    
                    if results:
                        print(f"[INFO] 成功生成 {len(results)} 张图片")
                        return results[0] if n == 1 else results
                    else:
                        raise Exception("响应中未找到有效的图片")
                else:
                    raise Exception(f"响应中没有 images 字段。Message 属性: {dir(message)}")
            
            else:
                # 其他供应商（Gemini等）：统一使用 litellm.completion()
                from litellm import completion
                
                print(f"[INFO] 使用 litellm.completion() 方式")
                
                # 构建 content
                if has_reference:
                    # 有参考图：构建多模态 content
                    content = [{"type": "text", "text": prompt}]
                    
                    for img_path_str in reference_images:
                        img_path = Path(img_path_str)
                        
                        # 读取并编码图片
                        with open(img_path, "rb") as image_file:
                            image_data = base64.b64encode(image_file.read()).decode('utf-8')
                        
                        # 判断图片格式
                        suffix = img_path.suffix.lower()
                        mime_type_map = {
                            '.jpg': 'image/jpeg',
                            '.jpeg': 'image/jpeg',
                            '.png': 'image/png',
                            '.gif': 'image/gif',
                            '.webp': 'image/webp'
                        }
                        mime_type = mime_type_map.get(suffix, 'image/jpeg')
                        
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_data}"
                            }
                        })
                else:
                    # 纯文本生成
                    content = prompt
                
                # 构建请求参数
                kwargs = {
                    "model": model,
                    "messages": [{"role": "user", "content": content}],
                    "api_key": self.api_key,
                    "timeout": 300,
                    "modalities": ["image", "text"]
                }
                
                # 添加 base_url（如果有）
                if self.base_url and self.base_url.strip():
                    kwargs["api_base"] = self.base_url
                
                # 添加 image_config（宽高比配置）
                if size and "x" in size:
                    width, height = map(int, size.split("x"))
                    from math import gcd
                    g = gcd(width, height)
                    ratio_w, ratio_h = width // g, height // g
                    aspect_ratio = f"{ratio_w}:{ratio_h}"
                    if aspect_ratio in ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]:
                        kwargs["image_config"] = {"aspect_ratio": aspect_ratio}
                
                # 调用 litellm.completion
                response = completion(**kwargs)
                
                # 提取图片
                results = []
                if hasattr(response, 'choices') and response.choices:
                    message = response.choices[0].message
                    
                    # 方式1：images 字段
                    if hasattr(message, 'images') and message.images:
                        for image in message.images:
                            if isinstance(image, dict):
                                image_url = image.get('image_url', {}).get('url')
                                if image_url:
                                    results.append(image_url)
                            elif hasattr(image, 'image_url'):
                                url = getattr(image.image_url, 'url', None)
                                if url:
                                    results.append(url)
                    
                    # 方式2：content 中的 image_url
                    if not results and hasattr(message, 'content'):
                        if isinstance(message.content, list):
                            for part in message.content:
                                if isinstance(part, dict) and part.get('type') == 'image_url':
                                    url = part.get('image_url', {}).get('url')
                                    if url:
                                        results.append(url)
                
                if results:
                    print(f"[INFO] 成功生成 {len(results)} 张图片")
                    return results[0] if n == 1 else results
                else:
                    if hasattr(response, 'choices') and response.choices:
                        message = response.choices[0].message
                        raise Exception(f"响应中未找到图片。Message 内容: {message.content[:200] if hasattr(message, 'content') else 'N/A'}")
                    else:
                        raise Exception("响应格式异常")
                
        except Exception as e:
            raise Exception(f"生成图片失败: {str(e)}")
    
    def audio_query(
        self,
        audio_path: str,
        question: str = "请描述这段音频的内容",
        model: Optional[str] = None
    ) -> str:
        """
        调用Audio模型分析音频
        
        Args:
            audio_path: 音频文件路径（绝对路径）
            question: 要问的问题
            model: 模型名称，默认使用配置中的第一个可用模型
            
        Returns:
            LLM的响应文本（包含转录内容和分析结果）
            
        Raises:
            FileNotFoundError: 音频文件不存在
            Exception: LLM调用失败
        
        流程:
        1. 使用 Whisper API 将音频转录为文本
        2. 根据问题分析转录内容并返回结果
        """
        # 检查音频文件
        audio_file = Path(audio_path)
        if not audio_file.exists():
            raise FileNotFoundError(f"音频文件不存在: {audio_path}")
        
        # 判断音频格式
        suffix = audio_file.suffix.lower()
        supported_formats = {
            '.mp3': 'audio/mpeg',
            '.mp4': 'audio/mp4',
            '.mpeg': 'audio/mpeg',
            '.mpga': 'audio/mpeg',
            '.m4a': 'audio/mp4',
            '.wav': 'audio/wav',
            '.webm': 'audio/webm'
        }
        
        if suffix not in supported_formats:
            raise ValueError(f"不支持的音频格式: {suffix}。支持的格式: {', '.join(supported_formats.keys())}")
        
        # 选择模型
        if model is None:
            model = self.models[0]
        
        try:
            # 步骤1: 转录音频为文本
            print(f"📝 正在转录音频: {audio_path}")
            
            transcript_text = ""
            
            if HAS_TRANSCRIBE:
                # 使用 litellm 的 transcribe 功能
                transcript = litellm.transcribe(
                    model="whisper-1",
                    file=str(audio_file),
                    api_key=self.api_key,
                    api_base=self.base_url,
                    timeout=300  # 5分钟超时保护
                )
                
                # 提取转录文本
                if isinstance(transcript, dict) and 'text' in transcript:
                    transcript_text = transcript['text']
                elif isinstance(transcript, str):
                    transcript_text = transcript
                else:
                    transcript_text = str(transcript)
            
            elif HAS_OPENAI:
                # 使用 OpenAI 直接调用
                with open(audio_file, "rb") as f:
                    transcript = openai.Audio.transcribe(
                        "whisper-1",
                        f,
                        api_key=self.api_key,
                        api_base=self.base_url if self.base_url else None
                    )
                    transcript_text = transcript['text']
            
            else:
                raise Exception("未安装必要的库（litellm 或 openai）")
            
            print(f"✅ 转录完成，文本长度: {len(transcript_text)} 字符")
            
            # 步骤2: 对转录内容进行分析
            messages = [{
                "role": "user",
                "content": f"以下是音频转录内容：\n\n{transcript_text}\n\n请回答以下问题：{question}"
            }]
            
            response = completion(
                model=model,
                messages=messages,
                temperature=self.temperature,
                api_key=self.api_key,
                api_base=self.base_url,
                timeout=300  # 5分钟超时保护
            )
            
            # 提取响应
            if response.choices and len(response.choices) > 0:
                analysis_result = response.choices[0].message.content
                
                # 返回包含转录和分析的完整结果
                return f"【音频转录】\n{transcript_text}\n\n【分析结果】\n{analysis_result}"
            else:
                raise Exception("LLM响应格式异常：缺少choices字段")
                
        except Exception as e:
            raise Exception(f"调用音频分析API失败: {str(e)}")
    
    def text_query(
        self,
        text: str,
        question: str,
        model: Optional[str] = None
    ) -> str:
        """
        通用文本分析（适用于论文、文档等长文本）
        
        Args:
            text: 要分析的文本内容
            question: 问题或指令
            model: 模型名称，默认使用配置中的第一个可用模型
            
        Returns:
            LLM的响应文本
            
        Raises:
            Exception: LLM调用失败
        """
        # 构建消息
        messages = [{
            "role": "user",
            "content": f"以下是内容：\n\n{text}\n\n{question}"
        }]
        
        # 选择模型
        if model is None:
            model = self.models[0]
        
        # 调用LLM
        try:
            response = completion(
                model=model,
                messages=messages,
                temperature=self.temperature,
                api_key=self.api_key,
                api_base=self.base_url,
                timeout=300  # 5分钟超时保护
            )
            
            # 提取响应
            if response.choices and len(response.choices) > 0:
                return response.choices[0].message.content
            else:
                raise Exception("LLM响应格式异常：缺少choices字段")
                
        except Exception as e:
            raise Exception(f"调用LLM文本分析API失败: {str(e)}")


# 全局单例（延迟初始化，支持配置文件热重载）
_client_instance: Optional[LLMClientLite] = None
_config_file_path: Optional[str] = None
_config_file_mtime: Optional[float] = None


def get_llm_client(force_reload: bool = False) -> LLMClientLite:
    """
    获取LLM客户端单例（支持配置文件热重载）
    
    Args:
        force_reload: 是否强制重新加载配置
    
    Returns:
        LLMClientLite实例
        
    Note:
        - 自动检测配置文件修改时间，如果配置文件被修改，会自动重新加载
        - 也可以通过 force_reload=True 强制重新加载
    """
    global _client_instance, _config_file_path, _config_file_mtime
    
    # 确定配置文件路径
    if _config_file_path is None:
        current_dir = Path(__file__).parent
        _config_file_path = str(current_dir.parent / "config" / "run_env_config" / "llm_config.yaml")
    
    # 检查配置文件是否存在
    if not os.path.exists(_config_file_path):
        if _client_instance is None:
            raise FileNotFoundError(f"配置文件不存在: {_config_file_path}")
        # 配置文件不存在但已有实例，返回现有实例
        return _client_instance
    
    # 获取当前配置文件的修改时间
    current_mtime = os.path.getmtime(_config_file_path)
    
    # 判断是否需要重新加载
    need_reload = (
        force_reload or 
        _client_instance is None or 
        _config_file_mtime is None or 
        current_mtime != _config_file_mtime
    )
    
    if need_reload:
        if _config_file_mtime is not None and current_mtime != _config_file_mtime:
            print(f"🔄 检测到配置文件变化，重新加载配置...")
        
        _client_instance = LLMClientLite()
        _config_file_mtime = current_mtime
    
    return _client_instance


def reload_llm_client() -> LLMClientLite:
    """
    强制重新加载LLM客户端配置
    
    Returns:
        重新加载后的LLMClientLite实例
    """
    return get_llm_client(force_reload=True)


if __name__ == "__main__":
    # 测试LLM客户端 - 图片编辑功能
    try:
        client = get_llm_client()
        print(f"✅ LLM客户端初始化成功")
        print(f"   可用模型: {client.models}")
        print(f"   图片生成模型: {client.figure_models}")
        print(f"   Base URL: {client.base_url}")
        print("\n" + "="*60)
        
        # 测试图片生成（带参考图）：融合两张图
        print("\n🎨 测试图片生成功能（带参考图）：融合两张图表...")
        
        image1_path = "/Users/chenglin/Desktop/research/agent_framwork/vscode_version/web-use/test_image/7.1.png"
        image2_path = "/Users/chenglin/Desktop/research/agent_framwork/vscode_version/web-use/test_image/7.2.png"
        
        prompt = """
        请将这两张数据可视化图表融合成一张综合图表。
        
        要求：
        1. 保留两张图的核心信息和数据点
        2. 使用统一的配色方案
        3. 合理布局，上下或左右排列
        4. 添加清晰的标题说明这是算法性能对比分析
        5. 确保图例和坐标轴标签清晰可读
        """
        
        output_path = "/Users/chenglin/Desktop/research/agent_framwork/vscode_version/web-use/test_image/7_merged.png"
        
        print(f"📷 参考图片1: {image1_path}")
        print(f"📷 参考图片2: {image2_path}")
        print(f"💾 输出路径: {output_path}")
        print(f"📝 提示词: {prompt.strip()[:100]}...")
        
        result = client.create_image(
            prompt=prompt,
            reference_images=[image1_path, image2_path],
            size="1792x1024",  # 16:9 比例，适合宽屏展示
            n=1,
            response_format="b64_json"
        )
        
        # 保存结果
        import base64
        if isinstance(result, str):
            # 单个结果
            if result.startswith("data:"):
                # base64 格式
                image_data = result.split(",")[1]
            else:
                image_data = result
            
            image_bytes = base64.b64decode(image_data)
            with open(output_path, 'wb') as f:
                f.write(image_bytes)
            
            print(f"\n✅ 图片编辑成功！")
            print(f"   保存位置: {output_path}")
            print(f"   文件大小: {len(image_bytes) / 1024:.2f} KB")
        else:
            # 多个结果
            print(f"\n✅ 生成了 {len(result)} 张图片")
            for idx, img_data in enumerate(result):
                save_path = output_path.replace(".png", f"_{idx}.png")
                if img_data.startswith("data:"):
                    img_data = img_data.split(",")[1]
                image_bytes = base64.b64decode(img_data)
                with open(save_path, 'wb') as f:
                    f.write(image_bytes)
                print(f"   图片 {idx+1}: {save_path}")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

