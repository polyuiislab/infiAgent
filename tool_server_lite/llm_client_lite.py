#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量级多模态LLM客户端 - 专供tool_server使用
支持：文本、图片、音频等多模态输入
"""

from __future__ import annotations

import os
import yaml
import base64
from pathlib import Path
from typing import Optional
from litellm import completion
import litellm

from utils.user_paths import ensure_user_llm_config_exists

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
            llm_config_path = str(ensure_user_llm_config_exists())
        
        if not os.path.exists(llm_config_path):
            raise FileNotFoundError(f"LLM配置文件不存在: {llm_config_path}")
        
        # 保存配置文件路径（用于后续可能的重载）
        self.config_path = llm_config_path
        
        with open(llm_config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # 读取配置
        self.base_url = self.config.get("base_url", "")
        self.api_key = self.config.get("api_key", "")
        self.temperature = self.config.get("temperature", 0)
        self.max_tokens = self.config.get("max_tokens", 0)
        
        # 解析模型配置（支持字符串和对象格式，对象格式可覆盖 api_key/base_url）
        self.model_configs = {}  # 模型名称 -> {api_key?, base_url?, ...}
        self.default_models = {}
        self.models = []
        self.figure_models = []
        self.compressor_models = []
        self.read_figure_models = []
        self._parse_models(self.config.get("models", []), self.models, "execution")
        self._parse_models(self.config.get("figure_models", []), self.figure_models, "image_generation")
        self._parse_models(self.config.get("compressor_models", []), self.compressor_models, "compressor")
        self._parse_models(self.config.get("read_figure_models", []), self.read_figure_models, "read_figure")
        
        # 回退逻辑：未配置的模型类别回退到 models
        if not self.figure_models:
            self.figure_models = list(self.models)
        if not self.compressor_models:
            self.compressor_models = list(self.models)
        if not self.read_figure_models:
            self.read_figure_models = list(self.models)
        self.default_models.setdefault("execution", self.models[0] if self.models else "")
        self.default_models.setdefault("image_generation", self.figure_models[0] if self.figure_models else "")
        self.default_models.setdefault("compressor", self.compressor_models[0] if self.compressor_models else "")
        self.default_models.setdefault("read_figure", self.read_figure_models[0] if self.read_figure_models else "")
        
        if not self.api_key and not any(cfg.get('api_key') for cfg in self.model_configs.values()):
            raise ValueError("未配置API密钥（可配置默认密钥或模型独立密钥）")
        
        if not self.models:
            raise ValueError("未配置可用模型列表")
        
        # 配置LiteLLM
        litellm.set_verbose = False
        litellm.drop_params = True
        
        print(f"✅ LLM客户端配置已加载: {llm_config_path}")
    
    def _parse_models(self, models_config: list, target_list: list, category: str):
        """解析模型配置，支持字符串和对象格式"""
        default_name = ""
        for item in models_config:
            if isinstance(item, str):
                target_list.append(item)
                if item not in self.model_configs:
                    self.model_configs[item] = {}
                if not default_name:
                    default_name = item
            elif isinstance(item, dict):
                name = item.get("name")
                if name:
                    target_list.append(name)
                    self.model_configs[name] = {k: v for k, v in item.items() if k != "name"}
                    if self.model_configs[name].get("default") is True:
                        default_name = name
                    elif not default_name:
                        default_name = name
        if default_name:
            self.default_models[category] = default_name

    @staticmethod
    def _record_response_usage(response, model, purpose):
        from utils.execution_scope import record_usage
        usage = getattr(response, 'usage', None)
        if hasattr(usage, 'model_dump'):
            usage = usage.model_dump()
        elif usage is not None and not isinstance(usage, dict):
            usage = dict(usage)
        record_usage(usage, model=str(model), purpose=purpose)

    def resolve_model(self, category: str = "execution", preferred: Optional[str] = None) -> str:
        model_groups = {
            "execution": self.models,
            "image_generation": self.figure_models or self.models,
            "compressor": self.compressor_models or self.models,
            "read_figure": self.read_figure_models or self.models,
        }
        candidates = model_groups.get(category, self.models) or self.models
        preferred_name = str(preferred or "").strip()
        if preferred_name and preferred_name in candidates:
            return preferred_name
        default_name = str(self.default_models.get(category) or "").strip()
        if default_name and default_name in candidates:
            return default_name
        return candidates[0] if candidates else preferred_name
    
    def _get_model_api_key(self, model: str) -> str:
        """获取模型的 api_key（优先模型级别，回退全局）"""
        return self.model_configs.get(model, {}).get("api_key", self.api_key)
    
    def _get_model_base_url(self, model: str) -> str:
        """获取模型的 base_url（优先模型级别，回退全局）"""
        return self.model_configs.get(model, {}).get("base_url", self.base_url)

    @staticmethod
    def _is_openrouter_base_url(base_url: Optional[str]) -> bool:
        return bool(base_url and 'openrouter' in str(base_url).lower())

    @staticmethod
    def _normalize_openrouter_model_name(model: str, base_url: Optional[str]) -> str:
        normalized = str(model or "").strip()
        if not normalized:
            return normalized
        if not LLMClientLite._is_openrouter_base_url(base_url):
            return normalized
        if normalized.startswith("openrouter/"):
            return normalized
        return f"openrouter/{normalized}"

    @staticmethod
    def _extract_image_urls_from_completion_message(message) -> list[str]:
        results: list[str] = []
        if message is None:
            return results

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

        if not results and hasattr(message, 'content') and isinstance(message.content, list):
            for part in message.content:
                if isinstance(part, dict) and part.get('type') == 'image_url':
                    url = part.get('image_url', {}).get('url')
                    if url:
                        results.append(url)

        return results

    def _create_image_via_litellm_completion(
        self,
        prompt: str,
        model: str,
        reference_images: Optional[list[str]] = None,
        size: str = "1024x1024",
    ) -> str | list[str]:
        has_reference = reference_images and len(reference_images) > 0

        content = [{"type": "text", "text": prompt}] if has_reference else prompt
        if has_reference:
            for img_path_str in reference_images:
                img_path = Path(img_path_str)
                with open(img_path, "rb") as image_file:
                    image_data = base64.b64encode(image_file.read()).decode('utf-8')

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

        model_api_key = self._get_model_api_key(model)
        model_base_url = self._get_model_base_url(model)
        normalized_model = self._normalize_openrouter_model_name(model, model_base_url)

        kwargs = {
            "model": normalized_model,
            "messages": [{"role": "user", "content": content}],
            "api_key": model_api_key,
            "timeout": 300,
            "modalities": ["image", "text"]
        }

        if model_base_url and model_base_url.strip():
            kwargs["api_base"] = model_base_url

        if size and "x" in size:
            width, height = map(int, size.split("x"))
            from math import gcd
            g = gcd(width, height)
            ratio_w, ratio_h = width // g, height // g
            aspect_ratio = f"{ratio_w}:{ratio_h}"
            if aspect_ratio in ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]:
                kwargs["extra_body"] = {"image_config": {"aspect_ratio": aspect_ratio}}

        from utils.execution_scope import check_cancelled
        check_cancelled()
        response = completion(**kwargs)
        self._record_response_usage(response, model, 'image_generation')
        if not getattr(response, 'choices', None):
            raise Exception("响应格式异常")

        message = response.choices[0].message
        results = self._extract_image_urls_from_completion_message(message)
        if results:
            print(f"[INFO] LiteLLM 成功生成 {len(results)} 张图片")
            return results[0] if len(results) == 1 else results

        raise Exception(
            f"LiteLLM completion 未在响应中找到图片。"
            f" model={getattr(response, 'model', 'unknown')}, "
            f"content_type={type(getattr(message, 'content', None)).__name__}"
        )

    def _create_image_via_openrouter_sdk(
        self,
        prompt: str,
        model: str,
        reference_images: Optional[list[str]] = None,
        size: str = "1024x1024",
        n: int = 1,
    ) -> str | list[str]:
        has_reference = reference_images and len(reference_images) > 0

        from openai import OpenAI

        client = OpenAI(
            base_url=self._get_model_base_url(model),
            api_key=self._get_model_api_key(model),
        )

        if has_reference:
            content = [{"type": "text", "text": prompt}]

            for img_path_str in reference_images:
                img_path = Path(img_path_str)
                if not img_path.exists():
                    raise FileNotFoundError(f"参考图片不存在: {img_path_str}")

                with open(img_path, "rb") as image_file:
                    image_data = base64.b64encode(image_file.read()).decode('utf-8')

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
            content = prompt

        extra_body = {"modalities": ["image", "text"]}

        if size and "x" in size:
            width, height = map(int, size.split("x"))
            from math import gcd
            g = gcd(width, height)
            ratio_w, ratio_h = width // g, height // g
            aspect_ratio = f"{ratio_w}:{ratio_h}"
            if aspect_ratio in ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]:
                extra_body["image_config"] = {"aspect_ratio": aspect_ratio}

        from utils.execution_scope import check_cancelled
        check_cancelled()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            extra_body=extra_body
        )
        self._record_response_usage(response, model, 'image_generation')

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
            print(f"[INFO] OpenRouter SDK 成功生成 {len(results)} 张图片")
            return results[0] if n == 1 else results

        raise Exception(f"响应中没有 images 字段。Message 属性: {dir(message)}")
    
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
        self.temperature = self.config.get("temperature", 0)
        self.max_tokens = self.config.get("max_tokens", 0)
        
        # 重新解析模型配置
        self.model_configs = {}
        self.models = []
        self.figure_models = []
        self.compressor_models = []
        self.read_figure_models = []
        self.default_models = {}
        self._parse_models(self.config.get("models", []), self.models, "execution")
        self._parse_models(self.config.get("figure_models", []), self.figure_models, "image_generation")
        self._parse_models(self.config.get("compressor_models", []), self.compressor_models, "compressor")
        self._parse_models(self.config.get("read_figure_models", []), self.read_figure_models, "read_figure")
        
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
            model = self.resolve_model("read_figure")
        
        # 调用LLM
        try:
            from utils.execution_scope import check_cancelled
            check_cancelled()
            response = completion(
                model=model,
                messages=messages,
                temperature=self.temperature,
                api_key=self._get_model_api_key(model),
                api_base=self._get_model_base_url(model),
                timeout=300  # 5分钟超时保护
            )
            self._record_response_usage(response, model, 'read_figure')
            
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
            if self.config.get('figure_models') and self.figure_models:
                model = self.resolve_model("image_generation")
            else:
                raise ValueError('请先配置文生图模型（figure_models）')
        
        try:
            has_reference = reference_images and len(reference_images) > 0
            print(f"[INFO] 调用图片生成 API: {model}")
            if has_reference:
                print(f"[INFO] 参考图片数量: {len(reference_images)}")
            if self.base_url:
                print(f"[INFO] 使用自定义端点: {self.base_url}")
            
            # 判断是否是 OpenRouter
            model_base_url = self._get_model_base_url(model)
            is_openrouter = self._is_openrouter_base_url(model_base_url)
            mode = self.model_configs.get(model, {}).get('image_api', 'auto')
            tail = model.rsplit('/', 1)[-1]
            native = mode == 'images' or (mode == 'auto' and not is_openrouter and
                      model.startswith('openai/') and tail.startswith(('gpt-image', 'dall-e')))
            if native:
                if reference_images:
                    raise ValueError('该图片接口当前只支持文生图；参考图编辑请使用支持图片输入的聊天多模态接口')
                from litellm import image_generation
                from utils.execution_scope import check_cancelled
                check_cancelled()
                options = {'model': model, 'prompt': prompt, 'n': n, 'size': size,
                           'api_key': self._get_model_api_key(model), 'api_base': model_base_url, 'timeout': 300}
                if not tail.startswith('gpt-image'):
                    options['response_format'] = response_format
                response = image_generation(**options)
                self._record_response_usage(response, model, 'image_generation')
                results = []
                for item in getattr(response, 'data', []) or []:
                    encoded = item.get('b64_json') if isinstance(item, dict) else getattr(item, 'b64_json', None)
                    url = item.get('url') if isinstance(item, dict) else getattr(item, 'url', None)
                    if encoded:
                        results.append('data:image/png;base64,' + encoded)
                    elif url:
                        results.append(url)
                if not results:
                    raise ValueError('图片接口没有返回图片数据')
                return results[0] if len(results) == 1 else results
            
            if is_openrouter:
                # OpenRouter: 优先用 LiteLLM completion 统一处理；若当前运行环境/返回结构不稳定，再回退原生 SDK。
                print(f"[INFO] 使用 OpenRouter 方式（LiteLLM 优先，SDK 兜底）")
                try:
                    return self._create_image_via_litellm_completion(
                        prompt=prompt,
                        model=model,
                        reference_images=reference_images,
                        size=size,
                    )
                except Exception as litellm_error:
                    print(f"[WARN] LiteLLM OpenRouter 图片生成失败，回退 SDK: {litellm_error}")
                    return self._create_image_via_openrouter_sdk(
                        prompt=prompt,
                        model=model,
                        reference_images=reference_images,
                        size=size,
                        n=n,
                    )
            
            else:
                # 其他供应商（Gemini等）：统一使用 litellm.completion()
                print(f"[INFO] 使用 litellm.completion() 方式")
                return self._create_image_via_litellm_completion(
                    prompt=prompt,
                    model=model,
                    reference_images=reference_images,
                    size=size,
                )
                
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
            
            from utils.execution_scope import check_cancelled
            check_cancelled()
            response = completion(
                model=model,
                messages=messages,
                temperature=self.temperature,
                api_key=self._get_model_api_key(model),
                api_base=self._get_model_base_url(model),
                timeout=300  # 5分钟超时保护
            )
            self._record_response_usage(response, model, 'audio')
            
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
            from utils.execution_scope import check_cancelled
            check_cancelled()
            response = completion(
                model=model,
                messages=messages,
                temperature=self.temperature,
                api_key=self._get_model_api_key(model),
                api_base=self._get_model_base_url(model),
                timeout=300  # 5分钟超时保护
            )
            self._record_response_usage(response, model, 'tool_text')
            
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


import threading
_client_lock = threading.RLock()
_client_cache = {}


def get_llm_client(force_reload: bool = False) -> LLMClientLite:
    path = Path(ensure_user_llm_config_exists()).resolve()
    stat = path.stat()
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    with _client_lock:
        previous = _client_cache.get(str(path))
        if force_reload or previous is None or previous[0] != signature:
            _client_cache[str(path)] = (signature, LLMClientLite(str(path)))
        return _client_cache[str(path)][1]


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
