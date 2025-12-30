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
                api_base=self.base_url
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
        model: Optional[str] = None
    ) -> str:
        """
        调用模型生成图片
        
        Args:
            prompt: 提示词
            model: 模型名称，默认使用 figure_models 中的第一个
            
        Returns:
            图片的 base64 数据 URL（格式：data:image/png;base64,...）或 HTTP URL
        """
        if model is None:
            if self.figure_models:
                # 兼容字符串或字典格式
                first_model = self.figure_models[0]
                model = first_model if isinstance(first_model, str) else first_model.get("name")
            else:
                model = "dall-e-3"
        
        try:
            # 判断使用哪种 API（通过 base_url 检测）
            # OpenRouter: 使用 chat.completions + modalities
            # 官方 API (gemini/, openai/): 使用 image_generation
            
            is_openrouter = self.base_url and 'openrouter' in self.base_url.lower()
            
            if is_openrouter:
                # OpenRouter: 使用 OpenAI SDK 的 chat completion + modalities
                from openai import OpenAI
                
                client = OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key
                )
                
                print(f"[INFO] 调用 OpenRouter 图片生成: {model}")
                
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    extra_body={"modalities": ["image", "text"]}
                )
                
                # 提取图片
                message = response.choices[0].message
                if hasattr(message, 'images') and message.images:
                    for image in message.images:
                        if isinstance(image, dict):
                            image_url = image.get('image_url', {}).get('url')
                            if image_url:
                                print(f"[INFO] 成功生成图片: {image_url[:50]}...")
                                return image_url
                    raise Exception("图片数据格式异常")
                else:
                    raise Exception(f"响应中没有图片。Message 属性: {dir(message)}")
            
            else:
                # 官方 API: 使用 litellm.image_generation
                from litellm import image_generation
                import os
                
                # 为官方 API 设置环境变量
                if model.startswith('gemini/'):
                    os.environ['GEMINI_API_KEY'] = self.api_key
                elif model.startswith('openai/') or model in ['dall-e-2', 'dall-e-3']:
                    os.environ['OPENAI_API_KEY'] = self.api_key
                
                print(f"[INFO] 调用官方 API 图片生成: {model}")
                
                response = image_generation(model=model, prompt=prompt)
                
                # 解析响应
                if response.data and len(response.data) > 0:
                    first_image = response.data[0]
                    
                    if hasattr(first_image, 'url') and first_image.url:
                        print(f"[INFO] 成功生成图片: {first_image.url[:100]}...")
                        return first_image.url
                    elif hasattr(first_image, 'b64_json') and first_image.b64_json:
                        data_url = f"data:image/png;base64,{first_image.b64_json}"
                        print(f"[INFO] 成功生成图片（base64），长度: {len(data_url)}")
                        return data_url
                    else:
                        raise Exception(f"图片响应格式异常: {first_image}")
                else:
                    raise Exception("模型未返回图片数据")
                
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
                    api_base=self.base_url
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
                api_base=self.base_url
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
                api_base=self.base_url
            )
            
            # 提取响应
            if response.choices and len(response.choices) > 0:
                return response.choices[0].message.content
            else:
                raise Exception("LLM响应格式异常：缺少choices字段")
                
        except Exception as e:
            raise Exception(f"调用LLM文本分析API失败: {str(e)}")


# 全局单例（延迟初始化）
_client_instance: Optional[LLMClientLite] = None


def get_llm_client() -> LLMClientLite:
    """
    获取LLM客户端单例
    
    Returns:
        LLMClientLite实例
    """
    global _client_instance
    if _client_instance is None:
        _client_instance = LLMClientLite()
    return _client_instance


if __name__ == "__main__":
    # 测试LLM客户端
    try:
        client = get_llm_client()
        print(f"✅ LLM客户端初始化成功")
        print(f"   可用模型: {client.models}")
        print(f"   Base URL: {client.base_url}")
        
        # 测试Vision调用（需要提供真实的图片路径）
        # result = client.vision_query("/path/to/image.jpg", "这是什么？")
        # print(f"✅ Vision响应: {result}")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

